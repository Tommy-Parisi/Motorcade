use std::collections::HashMap;
use std::sync::Mutex;
use std::time::{Duration, Instant};

use chrono::{DateTime, Utc};
use reqwest::Client;
use serde::Deserialize;

use crate::data::market_scanner::ScannedMarket;
use crate::execution::types::ExecutionError;
use crate::features::forecast::extract_threshold_from_ticker;

#[derive(Debug, Clone)]
pub struct EnrichmentConfig {
    pub ttl_secs: u64,
    pub noaa_point: String,
    pub sports_injury_api_url: Option<String>,
    pub sports_injury_api_key: Option<String>,
    pub crypto_sentiment_api_url: Option<String>,
    /// Optional esports win-probability API (e.g. PandaScore). Expected to accept
    /// a `title` query param and return `{"signal": <f64 in [-1,1]>}` where +1 means
    /// the market's stated team is heavily favoured. Set via ESPORTS_API_URL.
    pub esports_api_url: Option<String>,
}

impl Default for EnrichmentConfig {
    fn default() -> Self {
        Self {
            ttl_secs: 300,
            noaa_point: std::env::var("NOAA_POINT").unwrap_or_else(|_| "39.7456,-97.0892".to_string()),
            sports_injury_api_url: std::env::var("SPORTS_INJURY_API_URL").ok(),
            sports_injury_api_key: std::env::var("SPORTS_INJURY_API_KEY").ok(),
            crypto_sentiment_api_url: std::env::var("CRYPTO_SENTIMENT_API_URL")
                .ok()
                .or_else(|| Some("https://api.alternative.me/fng/?limit=1".to_string())),
            esports_api_url: std::env::var("ESPORTS_API_URL").ok(),
        }
    }
}

#[derive(Debug, Clone)]
pub struct MarketEnrichment {
    pub ticker: String,
    pub vertical: MarketVertical,
    pub weather_signal: Option<f64>,
    pub sports_injury_signal: Option<f64>,
    pub crypto_sentiment_signal: Option<f64>,
    /// Current coin price relative to the market's price threshold, normalized by ±5%.
    /// (current_price - threshold) / (threshold * 0.05), clamped to [-1, 1].
    /// None when the ticker has no extractable price threshold.
    pub crypto_price_signal: Option<f64>,
    /// Win-probability signal for esports markets, in [-1, 1].
    /// Populated when ESPORTS_API_URL is configured; None otherwise.
    pub esports_signal: Option<f64>,
    /// Financial instrument price vs threshold, normalized by ±2%.
    /// (current_price - threshold) / (threshold * 0.02), clamped to [-1, 1].
    /// Covers indices, commodities, forex. Fetched from Yahoo Finance (no key required).
    pub finance_price_signal: Option<f64>,
    pub generated_at: DateTime<Utc>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum MarketVertical {
    Weather,
    Sports,
    Esports,
    Finance,
    Politics,
    Crypto,
    Other,
}

pub struct MarketEnricher {
    cfg: EnrichmentConfig,
    http: Client,
    cache: Mutex<HashMap<String, CacheEntry>>,
    /// Short-lived price cache keyed by CoinGecko coin ID (e.g. "bitcoin").
    /// TTL is 60s — crypto prices change fast but we don't need per-tick precision.
    coin_price_cache: Mutex<HashMap<String, (Instant, f64)>>,
}

#[derive(Debug, Clone)]
struct CacheEntry {
    expires_at: Instant,
    data: MarketEnrichment,
}

impl MarketEnricher {
    pub fn new(cfg: EnrichmentConfig) -> Self {
        Self {
            cfg,
            http: Client::new(),
            cache: Mutex::new(HashMap::new()),
            coin_price_cache: Mutex::new(HashMap::new()),
        }
    }

    pub async fn enrich_batch(
        &self,
        markets: &[&ScannedMarket],
    ) -> Result<Vec<MarketEnrichment>, ExecutionError> {
        let mut out = Vec::with_capacity(markets.len());
        for m in markets {
            out.push(self.enrich_market(m).await?);
        }
        Ok(out)
    }

    pub async fn enrich_market(&self, market: &ScannedMarket) -> Result<MarketEnrichment, ExecutionError> {
        if let Some(cached) = self.get_cached(&market.ticker) {
            return Ok(cached);
        }

        let vertical = detect_vertical(market);
        let mut weather_signal = None;
        let mut sports_injury_signal = None;
        let mut crypto_sentiment_signal = None;
        let mut crypto_price_signal = None;
        let mut esports_signal = None;
        let mut finance_price_signal = None;

        match vertical {
            MarketVertical::Weather => {
                weather_signal = self.fetch_noaa_weather_signal(&market.ticker).await?;
            }
            MarketVertical::Sports => {
                sports_injury_signal = match self.fetch_sports_injury_signal().await {
                    Ok(v) => v,
                    Err(err) => {
                        eprintln!("enrichment warning: sports injury feed failed for {}: {}", market.ticker, err);
                        None
                    }
                };
            }
            MarketVertical::Esports => {
                esports_signal = match self.fetch_esports_signal(&market.title).await {
                    Ok(v) => v,
                    Err(err) => {
                        eprintln!("enrichment warning: esports feed failed for {}: {}", market.ticker, err);
                        None
                    }
                };
            }
            MarketVertical::Finance => {
                finance_price_signal = match self.fetch_finance_price_signal(&market.ticker).await {
                    Ok(v) => v,
                    Err(err) => {
                        eprintln!("enrichment warning: finance price feed failed for {}: {}", market.ticker, err);
                        None
                    }
                };
            }
            MarketVertical::Crypto => {
                crypto_sentiment_signal = match self.fetch_crypto_sentiment_signal().await {
                    Ok(v) => v,
                    Err(err) => {
                        eprintln!("enrichment warning: crypto sentiment feed failed for {}: {}", market.ticker, err);
                        None
                    }
                };
                crypto_price_signal = match self.fetch_crypto_price_signal(&market.ticker).await {
                    Ok(v) => v,
                    Err(err) => {
                        eprintln!("enrichment warning: crypto price feed failed for {}: {}", market.ticker, err);
                        None
                    }
                };
            }
            MarketVertical::Politics | MarketVertical::Other => {}
        }

        let enriched = MarketEnrichment {
            ticker: market.ticker.clone(),
            vertical,
            weather_signal,
            sports_injury_signal,
            crypto_sentiment_signal,
            crypto_price_signal,
            esports_signal,
            finance_price_signal,
            generated_at: Utc::now(),
        };
        self.put_cached(&market.ticker, enriched.clone());
        Ok(enriched)
    }

    fn get_cached(&self, ticker: &str) -> Option<MarketEnrichment> {
        let guard = self.cache.lock().ok()?;
        let e = guard.get(ticker)?;
        if Instant::now() < e.expires_at {
            Some(e.data.clone())
        } else {
            None
        }
    }

    fn put_cached(&self, ticker: &str, data: MarketEnrichment) {
        if let Ok(mut guard) = self.cache.lock() {
            guard.insert(
                ticker.to_string(),
                CacheEntry {
                    expires_at: Instant::now() + Duration::from_secs(self.cfg.ttl_secs),
                    data,
                },
            );
        }
    }

    async fn fetch_noaa_weather_signal(&self, ticker: &str) -> Result<Option<f64>, ExecutionError> {
        // Resolve city-specific coordinates from ticker prefix; fall back to config default.
        let point = extract_city_from_ticker(ticker)
            .and_then(city_coords)
            .unwrap_or(self.cfg.noaa_point.as_str());
        let points_url = format!("https://api.weather.gov/points/{}", point);
        let points = self
            .http
            .get(points_url)
            .header("User-Agent", "event-trading-bot (support@example.com)")
            .send()
            .await
            .map_err(|e| ExecutionError::RetryableExchange(e.to_string()))?;
        let points_status = points.status();
        let points_text = points
            .text()
            .await
            .map_err(|e| ExecutionError::Exchange(e.to_string()))?;
        if !points_status.is_success() {
            return Err(ExecutionError::Exchange(format!(
                "NOAA points failed ({points_status}): {points_text}"
            )));
        }
        let parsed_points: NoaaPointsResponse =
            serde_json::from_str(&points_text).map_err(|e| ExecutionError::Exchange(e.to_string()))?;
        let forecast_url = match parsed_points.properties.and_then(|p| p.forecast_hourly) {
            Some(url) => url,
            None => return Ok(None),
        };

        let forecast = self
            .http
            .get(forecast_url)
            .header("User-Agent", "event-trading-bot (support@example.com)")
            .send()
            .await
            .map_err(|e| ExecutionError::RetryableExchange(e.to_string()))?;
        let forecast_status = forecast.status();
        let forecast_text = forecast
            .text()
            .await
            .map_err(|e| ExecutionError::Exchange(e.to_string()))?;
        if !forecast_status.is_success() {
            return Err(ExecutionError::Exchange(format!(
                "NOAA forecast failed ({forecast_status}): {forecast_text}"
            )));
        }
        let parsed_forecast: NoaaForecastResponse =
            serde_json::from_str(&forecast_text).map_err(|e| ExecutionError::Exchange(e.to_string()))?;

        // Store raw temperature in °F so callers can normalize relative to a market-specific
        // threshold (e.g. "will high exceed 55°F?" → signal = forecast_temp - 55).
        let temp = parsed_forecast
            .properties
            .and_then(|p| p.periods)
            .and_then(|mut v| v.drain(..).next())
            .and_then(|p| p.temperature);
        Ok(temp)
    }

    async fn fetch_sports_injury_signal(&self) -> Result<Option<f64>, ExecutionError> {
        let Some(url) = &self.cfg.sports_injury_api_url else {
            return Ok(None);
        };
        let mut req = self.http.get(url);
        if let Some(key) = &self.cfg.sports_injury_api_key {
            req = req.header("Authorization", format!("Bearer {key}"));
        }
        let resp = req
            .send()
            .await
            .map_err(|e| ExecutionError::RetryableExchange(e.to_string()))?;
        let status = resp.status();
        let text = resp
            .text()
            .await
            .map_err(|e| ExecutionError::Exchange(e.to_string()))?;
        if !status.is_success() {
            return Err(ExecutionError::Exchange(format!(
                "sports injury feed failed ({status}): {text}"
            )));
        }
        let parsed: GenericSignalResponse =
            serde_json::from_str(&text).map_err(|e| ExecutionError::Exchange(e.to_string()))?;
        Ok(parsed.signal)
    }

    async fn fetch_crypto_sentiment_signal(&self) -> Result<Option<f64>, ExecutionError> {
        let Some(url) = &self.cfg.crypto_sentiment_api_url else {
            return Ok(None);
        };
        let resp = self
            .http
            .get(url)
            .send()
            .await
            .map_err(|e| ExecutionError::RetryableExchange(e.to_string()))?;
        let status = resp.status();
        let text = resp
            .text()
            .await
            .map_err(|e| ExecutionError::Exchange(e.to_string()))?;
        if !status.is_success() {
            return Err(ExecutionError::Exchange(format!(
                "crypto sentiment feed failed ({status}): {text}"
            )));
        }

        if let Ok(parsed) = serde_json::from_str::<FearGreedResponse>(&text) {
            let score = parsed
                .data
                .and_then(|mut d| d.drain(..).next())
                .and_then(|r| r.value)
                .map(|v| ((v / 100.0) * 2.0) - 1.0);
            return Ok(score);
        }

        let parsed: GenericSignalResponse =
            serde_json::from_str(&text).map_err(|e| ExecutionError::Exchange(e.to_string()))?;
        Ok(parsed.signal)
    }

    /// Fetch current coin price from CoinGecko and compute signal relative to the market's
    /// price threshold encoded in the ticker (e.g. `KXBTC-26MAR2617-B63325` → threshold=$63,325).
    ///
    /// Signal = (current_price - threshold) / (threshold × 0.05), clamped to [-1, 1].
    /// A signal of +1.0 means the coin is ≥5% above the threshold (strong YES); -1.0 means ≥5% below.
    /// Returns None when no threshold is extractable or the CoinGecko call fails.
    async fn fetch_crypto_price_signal(&self, ticker: &str) -> Result<Option<f64>, ExecutionError> {
        let threshold = match extract_threshold_from_ticker(ticker) {
            Some(t) if t > 0.0 => t,
            _ => return Ok(None),
        };
        let coin_id = match extract_coin_id(ticker) {
            Some(id) => id,
            None => return Ok(None),
        };

        // Check short-lived price cache (60s TTL — fine-grained enough for intraday signals)
        if let Ok(guard) = self.coin_price_cache.lock() {
            if let Some((expires, price)) = guard.get(coin_id) {
                if Instant::now() < *expires {
                    return Ok(Some(((price - threshold) / (threshold * 0.05)).clamp(-1.0, 1.0)));
                }
            }
        }

        let url = format!(
            "https://api.coingecko.com/api/v3/simple/price?ids={}&vs_currencies=usd",
            coin_id
        );
        let resp = self
            .http
            .get(&url)
            .header("User-Agent", "event-trading-bot (support@example.com)")
            .send()
            .await
            .map_err(|e| ExecutionError::RetryableExchange(e.to_string()))?;
        let status = resp.status();
        let text = resp
            .text()
            .await
            .map_err(|e| ExecutionError::Exchange(e.to_string()))?;
        if !status.is_success() {
            // Don't hard-fail enrichment for a price feed outage — return None gracefully.
            return Ok(None);
        }

        let prices: HashMap<String, HashMap<String, f64>> =
            match serde_json::from_str(&text) {
                Ok(v) => v,
                Err(_) => return Ok(None),
            };

        let price = prices.get(coin_id).and_then(|m| m.get("usd")).copied();
        if let Some(p) = price {
            if let Ok(mut guard) = self.coin_price_cache.lock() {
                guard.insert(coin_id.to_string(), (Instant::now() + Duration::from_secs(60), p));
            }
            return Ok(Some(((p - threshold) / (threshold * 0.05)).clamp(-1.0, 1.0)));
        }
        Ok(None)
    }

    /// Fetch the current price of a financial instrument (index, commodity, forex) from
    /// Yahoo Finance and compute a signal relative to the market's threshold.
    ///
    /// Signal = (current_price - threshold) / (threshold × 0.02), clamped to [-1, 1].
    /// 2% normalization: indices/commodities don't move 5% intraday like crypto.
    /// Uses the same 60s coin_price_cache (keyed by Yahoo symbol) to avoid hammering Yahoo.
    async fn fetch_finance_price_signal(&self, ticker: &str) -> Result<Option<f64>, ExecutionError> {
        let threshold = match extract_threshold_from_ticker(ticker) {
            Some(t) if t > 0.0 => t,
            _ => return Ok(None),
        };
        let symbol = match extract_finance_symbol(ticker) {
            Some(s) => s,
            None => return Ok(None),
        };

        if let Ok(guard) = self.coin_price_cache.lock() {
            if let Some((expires, price)) = guard.get(symbol) {
                if Instant::now() < *expires {
                    return Ok(Some(((price - threshold) / (threshold * 0.02)).clamp(-1.0, 1.0)));
                }
            }
        }

        let url = format!(
            "https://query1.finance.yahoo.com/v8/finance/chart/{}?interval=1d&range=1d",
            symbol
        );
        let resp = self
            .http
            .get(&url)
            .header("User-Agent", "event-trading-bot (support@example.com)")
            .send()
            .await
            .map_err(|e| ExecutionError::RetryableExchange(e.to_string()))?;
        let status = resp.status();
        let text = resp
            .text()
            .await
            .map_err(|e| ExecutionError::Exchange(e.to_string()))?;
        if !status.is_success() {
            return Ok(None);
        }

        // Yahoo Finance v8 response: chart.result[0].meta.regularMarketPrice
        let parsed: serde_json::Value = match serde_json::from_str(&text) {
            Ok(v) => v,
            Err(_) => return Ok(None),
        };
        let price = parsed
            .pointer("/chart/result/0/meta/regularMarketPrice")
            .and_then(|v| v.as_f64());

        if let Some(p) = price {
            if let Ok(mut guard) = self.coin_price_cache.lock() {
                guard.insert(symbol.to_string(), (Instant::now() + Duration::from_secs(60), p));
            }
            return Ok(Some(((p - threshold) / (threshold * 0.02)).clamp(-1.0, 1.0)));
        }
        Ok(None)
    }

    /// Fetch an esports win-probability signal for the given market title.
    ///
    /// Requires `ESPORTS_API_URL` to be configured (e.g. a PandaScore proxy).
    /// The API is called with `?title=<market title>` and must return
    /// `{"signal": <f64 in [-1, 1]>}` where +1 = strong favourite to win.
    /// Returns None when no URL is configured — vertical classification still
    /// applies even without the signal.
    async fn fetch_esports_signal(&self, title: &str) -> Result<Option<f64>, ExecutionError> {
        let Some(url) = &self.cfg.esports_api_url else {
            return Ok(None);
        };
        let resp = self
            .http
            .get(url)
            .query(&[("title", title)])
            .send()
            .await
            .map_err(|e| ExecutionError::RetryableExchange(e.to_string()))?;
        let status = resp.status();
        let text = resp
            .text()
            .await
            .map_err(|e| ExecutionError::Exchange(e.to_string()))?;
        if !status.is_success() {
            return Ok(None);
        }
        let parsed: GenericSignalResponse =
            serde_json::from_str(&text).map_err(|e| ExecutionError::Exchange(e.to_string()))?;
        Ok(parsed.signal)
    }
}

/// Map a finance ticker prefix to its Yahoo Finance symbol for price lookups.
fn extract_finance_symbol(ticker: &str) -> Option<&'static str> {
    let upper = ticker.to_ascii_uppercase();
    // Equity indices
    if upper.starts_with("KXNASDAQ100") { return Some("%5ENDX"); }  // ^NDX (URL-encoded)
    if upper.starts_with("KXINX")       { return Some("%5EGSPC"); } // ^GSPC S&P 500
    // Treasury yields
    if upper.starts_with("KXTNOTED")    { return Some("%5ETNX"); }  // ^TNX 10-year yield
    // Commodities
    if upper.starts_with("KXGOLDD")     { return Some("GC%3DF"); }  // GC=F Gold futures
    if upper.starts_with("KXSILVERD")   { return Some("SI%3DF"); }  // SI=F Silver futures
    if upper.starts_with("KXCOPPERD")   { return Some("HG%3DF"); }  // HG=F Copper futures
    if upper.starts_with("KXBRENT")     { return Some("BZ%3DF"); }  // BZ=F Brent crude
    if upper.starts_with("KXWTI")       { return Some("CL%3DF"); }  // CL=F WTI crude
    // Forex
    if upper.starts_with("KXEURUSD")    { return Some("EURUSD%3DX"); } // EURUSD=X
    if upper.starts_with("KXUSDJPY")    { return Some("JPY%3DX"); }    // JPY=X
    if upper.starts_with("KXUSDGBP") || upper.starts_with("KXGBPUSD") { return Some("GBPUSD%3DX"); }
    None
}

/// Map a crypto ticker to its CoinGecko coin ID for price lookups.
/// Returns None for unknown coins so that enrichment degrades gracefully.
fn extract_coin_id(ticker: &str) -> Option<&'static str> {
    let upper = ticker.to_ascii_uppercase();
    if upper.contains("BTC") || upper.contains("BITCOIN") {
        return Some("bitcoin");
    }
    if upper.contains("ETH") || upper.contains("ETHEREUM") {
        return Some("ethereum");
    }
    if upper.contains("SOL") || upper.contains("SOLANA") {
        return Some("solana");
    }
    if upper.contains("XRP") || upper.contains("RIPPLE") {
        return Some("ripple");
    }
    None
}

/// Returns (lat, lon) string for a known city code extracted from a KXHIGH* ticker.
/// City codes are the 2-4 letter suffix after "KXHIGH" (e.g. "KXHIGHCHI" → "CHI" → Chicago).
fn city_coords(city_code: &str) -> Option<&'static str> {
    match city_code.to_ascii_uppercase().as_str() {
        // US cities
        "ATL" => Some("33.7490,-84.3880"),   // Atlanta
        "AUS" => Some("30.2672,-97.7431"),   // Austin
        "BOS" => Some("42.3601,-71.0589"),   // Boston
        "CHI" => Some("41.8781,-87.6298"),   // Chicago
        "DAL" => Some("32.7767,-96.7970"),   // Dallas
        "DEN" => Some("39.7392,-104.9903"),  // Denver
        "DET" => Some("42.3314,-83.0458"),   // Detroit
        "HOU" => Some("29.7604,-95.3698"),   // Houston
        "KC"  => Some("39.0997,-94.5786"),   // Kansas City
        "LA"  | "LAX" => Some("34.0522,-118.2437"), // Los Angeles
        "LAS" | "LV"  => Some("36.1699,-115.1398"), // Las Vegas
        "MIA" => Some("25.7617,-80.1918"),   // Miami
        "MIL" => Some("43.0389,-87.9065"),   // Milwaukee
        "MIN" | "MSP" => Some("44.9778,-93.2650"),  // Minneapolis
        "NYC" | "NY"  => Some("40.7128,-74.0060"),  // New York City
        "ORL" => Some("28.5383,-81.3792"),   // Orlando
        "PHI" => Some("39.9526,-75.1652"),   // Philadelphia
        "PHX" => Some("33.4484,-112.0740"),  // Phoenix
        "PIT" => Some("40.4406,-79.9959"),   // Pittsburgh
        "PDX" => Some("45.5051,-122.6750"),  // Portland
        "SAC" => Some("38.5816,-121.4944"),  // Sacramento
        "SEA" => Some("47.6062,-122.3321"),  // Seattle
        "SF"  | "SFO" => Some("37.7749,-122.4194"), // San Francisco
        "SLC" => Some("40.7608,-111.8910"),  // Salt Lake City
        "STL" => Some("38.6270,-90.1994"),   // St. Louis
        "DC"  | "DCA" => Some("38.9072,-77.0369"),  // Washington DC
        _ => None,
    }
}

/// Extract the city code from a weather ticker like "KXHIGHCHI-26FEB16-T45" → "CHI".
/// Strips the "KXHIGH" prefix and takes the next alphabetic segment before any dash or digit.
fn extract_city_from_ticker(ticker: &str) -> Option<&str> {
    let upper = ticker.to_ascii_uppercase();
    let rest = upper.strip_prefix("KXHIGH")?;
    // Take the leading alphabetic segment (the city code)
    let end = rest.find(|c: char| !c.is_ascii_alphabetic()).unwrap_or(rest.len());
    if end == 0 {
        return None;
    }
    // Return a slice of the original ticker (not the uppercase copy) so lifetime is fine.
    // We need to find the same range in the original ticker.
    let prefix_len = "KXHIGH".len();
    Some(&ticker[prefix_len..prefix_len + end])
}

/// Select up to `limit` markets for enrichment using round-robin across verticals
/// (Weather → Sports → Crypto → Other, highest volume first within each).
///
/// Without this, top-N by volume can be entirely crypto or sports, leaving
/// weather markets — which benefit most from NOAA enrichment — with zero slots.
pub fn select_for_enrichment(markets: &[ScannedMarket], limit: usize) -> Vec<&ScannedMarket> {
    // Bucket priority: Weather(0) → Esports(1) → Finance(2) → Sports(3) → Politics(4) → Crypto(5) → Other(6)
    // Finance gets priority before Sports: its price signal is immediately actionable.
    let mut buckets: [Vec<&ScannedMarket>; 7] = Default::default();
    for m in markets {
        let idx = match detect_vertical(m) {
            MarketVertical::Weather  => 0,
            MarketVertical::Esports  => 1,
            MarketVertical::Finance  => 2,
            MarketVertical::Sports   => 3,
            MarketVertical::Politics => 4,
            MarketVertical::Crypto   => 5,
            MarketVertical::Other    => 6,
        };
        buckets[idx].push(m);
    }
    // Markets come in pre-sorted by volume desc from select_for_valuation, but sort
    // within each bucket to be explicit.
    for bucket in &mut buckets {
        bucket.sort_by(|a, b| b.volume.total_cmp(&a.volume));
    }

    let mut cursors = [0usize; 7];
    let mut result = Vec::with_capacity(limit);
    loop {
        let mut added_any = false;
        for (i, bucket) in buckets.iter().enumerate() {
            if result.len() >= limit {
                break;
            }
            if cursors[i] < bucket.len() {
                result.push(bucket[cursors[i]]);
                cursors[i] += 1;
                added_any = true;
            }
        }
        if !added_any || result.len() >= limit {
            break;
        }
    }
    result
}

pub fn detect_vertical(market: &ScannedMarket) -> MarketVertical {
    let text = format!("{} {}", market.ticker, market.title).to_ascii_lowercase();

    // Weather — unambiguous ticker prefixes first
    if text.contains("kxhigh")
        || text.contains("kxlow")
        || text.contains("temp")
        || text.contains("weather")
        || text.contains("rain")
        || text.contains("snow")
    {
        return MarketVertical::Weather;
    }

    // Esports — before general Sports to prevent title keywords like "winner" misrouting
    if text.contains("kxcs2")
        || text.contains("kxdota2")
        || text.contains("kxvalorant")
        || text.contains("kxlol")     // League of Legends
        || text.contains("kxow")      // Overwatch
        || text.contains("kxcod")     // Call of Duty
        || text.contains("kxgbl")     // Global Esports League
        || text.contains("kxbbl")     // BBL Esports
        || text.contains("kxballerleague")
        || text.contains("cs2")
        || text.contains("dota 2")
        || text.contains("valorant")
        || text.contains("league of legends")
        || text.contains("overwatch")
        || text.contains("esport")
    {
        return MarketVertical::Esports;
    }

    // Finance — indices, commodities, forex, economic data
    if text.contains("kxnasdaq")
        || text.contains("kxinx")      // S&P 500
        || text.contains("kxtnoted")   // Treasury notes
        || text.contains("kxgoldd")
        || text.contains("kxsilverd")
        || text.contains("kxcopperd")
        || text.contains("kxbrentd") || text.contains("kxbrentw")
        || text.contains("kxwti")
        || text.contains("kxeurusd") || text.contains("kxusdjpy") || text.contains("kxusdgbp")
        || text.contains("kxusgascpi")
        || text.contains("kxsheltercpi") || text.contains("kxairfarecpi") || text.contains("kxusedcarcpi")
        || text.contains("kxchcuts")   // Challenger job cuts
        || text.contains("kxaaagasw")  // AAA gas prices
        || text.contains("nasdaq")
        || text.contains("s&p 500")
        || text.contains("treasury")
        || (text.contains("gold") && text.contains("price"))
        || (text.contains("oil") && text.contains("price"))
        || text.contains("brent crude")
        || text.contains("wti")
        || text.contains("copper close price")
    {
        return MarketVertical::Finance;
    }

    // Politics — elections, legislation, government mentions
    if text.contains("kxgaprimary") || text.contains("kxnjprimary")
        || text.contains("kxpresnomd") || text.contains("kxpresnomr")
        || text.contains("kxsenate") || text.contains("kxgov")
        || text.contains("kxtrumpsay") || text.contains("kxtrumpmention") || text.contains("kxtrumpact")
        || text.contains("kxscotusmention")
        || text.contains("kxcongressmention")
        || text.contains("kxdhsfund") || text.contains("kxbillscount")
        || text.contains("kxfederalcharge")
        || text.contains("kxbulgaria") || text.contains("kxfaroe") || text.contains("kxderemerout")
        || text.contains("election") || text.contains("nominee") || text.contains("parliament")
        || text.contains("kxswencounters") // border encounters
    {
        return MarketVertical::Politics;
    }

    // Sports — North American major leagues + international leagues + motorsport + combat + golf
    if text.contains("nba") || text.contains("nfl") || text.contains("mlb") || text.contains("nhl")
        || text.contains("ncaa") || text.contains("ufc") || text.contains("mma") || text.contains("boxing")
        || text.contains("kxpga") || text.contains("kxpgar") || text.contains("golf") || text.contains("pga tour")
        || text.contains("kxf1") || text.contains("formula 1") || text.contains("kxnascar") || text.contains("nascar")
        || text.contains("kxmotogp") || text.contains("motogp")
        || text.contains("kxdp")   // DP World Tour
        || text.contains("kxufc") || text.contains("darts")
        // International soccer/football leagues
        || text.contains("kxepl") || text.contains("kxucl") || text.contains("kxewsl") || text.contains("kxnwsl")
        || text.contains("kxligamx") || text.contains("kxbrasileiro") || text.contains("kxargprem")
        || text.contains("kxargpremdiv") || text.contains("kxdimayo") || text.contains("kxurypdg")
        || text.contains("kxfifa") || text.contains("kxintlfriendly") || text.contains("kxjleague")
        || text.contains("kxlaliga") || text.contains("kxeflcup") || text.contains("kxligamx")
        || text.contains("kxafpfddhgame") || text.contains("kxeuroleague")
        // International hockey
        || text.contains("kxkhl") || text.contains("kxliiga") || text.contains("kxshl")
        || text.contains("kxahl") || text.contains("kxdel") || text.contains("kxnlgame")
        || text.contains("kxelh") || text.contains("kxvtb") || text.contains("kxafl")
        // International basketball
        || text.contains("kxarglnb") || text.contains("kxkbl") || text.contains("kxnbl")
        || text.contains("kxaba") || text.contains("kxeuroleague")
        // Tennis (both tours) — kxmomen/kxfomen match via "atp" in title;
        // kxmowomen/kxfowomen match via "wta" in title; catch all WTA/ATP title variants.
        || text.contains("kxwta") || text.contains("wta")
        || text.contains("tennis") || text.contains("atp")
        // Grand-slam tournament prefixes (French Open, US Open, etc.)
        || text.contains("kxfomen") || text.contains("kxfowomen")
        // Rugby / cricket / other
        || text.contains("kxrugbynrl") || text.contains("kxt20match")
        || text.contains("kxiplgame")  // IPL cricket
        || text.contains("cba")    // Chinese Basketball
        // Injury/sports generic
        || text.contains("injury") || text.contains("soccer")
    {
        return MarketVertical::Sports;
    }

    // Crypto — major coins + altcoins (DOGE, SHIBA, HYPE, BNB, MATIC, LINK, AVAX)
    if text.contains("kxbtc") || text.contains("kxeth") || text.contains("kxsol")
        || text.contains("kxxrp") || text.contains("kxdoge") || text.contains("kxshiba")
        || text.contains("kxhype") || text.contains("kxbnb") || text.contains("kxmatic")
        || text.contains("kxlink") || text.contains("kxavax")
        || text.contains("bitcoin") || text.contains("ethereum") || text.contains("crypto")
        || text.contains("shiba inu")
    {
        return MarketVertical::Crypto;
    }

    MarketVertical::Other
}

#[derive(Debug, Deserialize)]
struct NoaaPointsResponse {
    properties: Option<NoaaPointsProperties>,
}

#[derive(Debug, Deserialize)]
struct NoaaPointsProperties {
    #[serde(alias = "forecastHourly", alias = "forecast_hourly")]
    forecast_hourly: Option<String>,
}

#[derive(Debug, Deserialize)]
struct NoaaForecastResponse {
    properties: Option<NoaaForecastProperties>,
}

#[derive(Debug, Deserialize)]
struct NoaaForecastProperties {
    periods: Option<Vec<NoaaForecastPeriod>>,
}

#[derive(Debug, Deserialize)]
struct NoaaForecastPeriod {
    temperature: Option<f64>,
}

#[derive(Debug, Deserialize)]
struct GenericSignalResponse {
    signal: Option<f64>,
}

#[derive(Debug, Deserialize)]
struct FearGreedResponse {
    data: Option<Vec<FearGreedRecord>>,
}

#[derive(Debug, Deserialize)]
struct FearGreedRecord {
    value: Option<f64>,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn detects_market_verticals() {
        let weather = ScannedMarket {
            ticker: "KXWEATHER-NYC".to_string(),
            title: "NYC high temp above 90F".to_string(),
            subtitle: None,
            market_type: None,
            event_ticker: None,
            series_ticker: None,
            yes_bid_cents: None,
            yes_ask_cents: None,
            volume: 0.0,
            close_time: None,
        };
        let crypto = ScannedMarket {
            ticker: "KXBTC-TEST".to_string(),
            title: "Bitcoin above 120k".to_string(),
            subtitle: None,
            market_type: None,
            event_ticker: None,
            series_ticker: None,
            yes_bid_cents: None,
            yes_ask_cents: None,
            volume: 0.0,
            close_time: None,
        };
        assert_eq!(detect_vertical(&weather), MarketVertical::Weather);
        assert_eq!(detect_vertical(&crypto), MarketVertical::Crypto);
    }

    #[test]
    fn detect_vertical_esports_tickers() {
        for (ticker, title) in [
            ("KXCS2GAME-26MAR19HEROTRK-HERO", "Will Heroic win the Tricked vs. Heroic CS2 match?"),
            ("KXCS2MAP-26MAR19BWSTA-1-STA", "Will STATE win map 1?"),
            ("KXDOTA2MAP-26MAR20FOO-1-BAR", "Will Team win map 1 in Dota 2?"),
            ("KXVALORANTGAME-26MAR20SENA-SENB", "Will Sen win the Valorant match?"),
        ] {
            let m = mk_market(ticker, title, 1000.0);
            assert_eq!(detect_vertical(&m), MarketVertical::Esports,
                "expected Esports for ticker={ticker}");
        }
    }

    #[test]
    fn detect_vertical_expanded_sports() {
        for (ticker, title) in [
            ("KXF1POLE-26MAR26-VER", "Will Verstappen take pole at the 2026 Australian GP?"),
            ("KXNASCARRACE-26MAR26-HAM", "NASCAR Daytona winner"),
            ("KXMOTOGP-26MAR26-MAR", "MotoGP Valencia winner"),
            ("KXPGAMAKECUT-TECHO26-PKIZ", "Will Kizzire make the cut?"),
        ] {
            let m = mk_market(ticker, title, 1000.0);
            assert_eq!(detect_vertical(&m), MarketVertical::Sports,
                "expected Sports for ticker={ticker}");
        }
    }

    #[test]
    fn detect_vertical_expanded_crypto() {
        for ticker in ["KXDOGED-26MAR26-B0", "KXDOGE-26MAR26", "KXBNB-26APR", "KXAVAX-26APR"] {
            let m = mk_market(ticker, "crypto price market", 1000.0);
            assert_eq!(detect_vertical(&m), MarketVertical::Crypto,
                "expected Crypto for ticker={ticker}");
        }
    }

    #[test]
    fn detect_vertical_finance_markets() {
        for (ticker, title) in [
            ("KXNASDAQ100U-26MAR26", "Nasdaq 100 above X?"),
            ("KXEURUSD-26MAR26", "EUR/USD above 1.10?"),
            ("KXGOLDD-26MAR26", "Will the gold close price be above $3200?"),
            ("KXWTI-26MAR26", "Will the WTI price be above $80?"),
            ("KXINX-26MAR26-B5000", "Will the S&P 500 be above 5000?"),
        ] {
            let m = mk_market(ticker, title, 1000.0);
            assert_eq!(detect_vertical(&m), MarketVertical::Finance,
                "expected Finance for ticker={ticker}");
        }
    }

    #[test]
    fn detect_vertical_politics_markets() {
        for (ticker, title) in [
            ("KXTRUMPSAY-26MAR26", "Will Trump say X before Mar 30?"),
            ("KXGAPRIMARY-26MAR26", "Georgia primary result"),
            ("KXDHSFUND-26MAR26", "Will DHS funding bill become law?"),
            ("KXBULGARIAPARLI-26MAR26", "Will APS win the Bulgarian election?"),
        ] {
            let m = mk_market(ticker, title, 1000.0);
            assert_eq!(detect_vertical(&m), MarketVertical::Politics,
                "expected Politics for ticker={ticker}");
        }
    }

    #[test]
    fn detect_vertical_other_for_entertainment() {
        for (ticker, title) in [
            ("KXSPOTSTREAMGLOBAL-26MAR26", "How many streams will the top song have?"),
            ("KXALBUMSALES-26MAR26", "Will album have 20000 pure sales?"),
            ("KXTOPMODEL-26MAR26", "Top AI model this week?"),
        ] {
            let m = mk_market(ticker, title, 1000.0);
            assert_eq!(detect_vertical(&m), MarketVertical::Other,
                "expected Other for ticker={ticker}");
        }
    }

    #[test]
    fn extract_city_from_ticker_known_cities() {
        assert_eq!(extract_city_from_ticker("KXHIGHCHI-26FEB16-T45"), Some("CHI"));
        assert_eq!(extract_city_from_ticker("KXHIGHHOU-26MAR25-T80"), Some("HOU"));
        assert_eq!(extract_city_from_ticker("KXHIGHNYC-26APR01"), Some("NYC"));
        assert_eq!(extract_city_from_ticker("KXHIGHLA-TEST"), Some("LA"));
        assert_eq!(extract_city_from_ticker("KXHIGHSF-26MAY"), Some("SF"));
    }

    #[test]
    fn extract_city_from_ticker_non_high_ticker_returns_none() {
        assert_eq!(extract_city_from_ticker("KXBTC-26DEC"), None);
        assert_eq!(extract_city_from_ticker("KXNBAGAME-123"), None);
        assert_eq!(extract_city_from_ticker("KXHIGH"), None); // no city segment
    }

    #[test]
    fn city_coords_known_lookup() {
        assert_eq!(city_coords("CHI"), Some("41.8781,-87.6298"));
        assert_eq!(city_coords("NYC"), Some("40.7128,-74.0060"));
        assert_eq!(city_coords("HOU"), Some("29.7604,-95.3698"));
        assert_eq!(city_coords("SEA"), Some("47.6062,-122.3321"));
    }

    #[test]
    fn city_coords_case_insensitive() {
        assert_eq!(city_coords("chi"), city_coords("CHI"));
        assert_eq!(city_coords("nyc"), city_coords("NYC"));
    }

    #[test]
    fn city_coords_unknown_returns_none() {
        assert_eq!(city_coords("ZZZ"), None);
        assert_eq!(city_coords(""), None);
    }

    #[test]
    fn extract_city_from_ticker_roundtrip_to_coords() {
        // Full path: ticker → city code → coords
        let ticker = "KXHIGHCHI-26FEB16-T45";
        let city = extract_city_from_ticker(ticker).expect("should extract city");
        let coords = city_coords(city).expect("CHI should be in map");
        assert!(coords.contains("41.8781"), "should be Chicago latitude");
    }

    fn mk_market(ticker: &str, title: &str, volume: f64) -> ScannedMarket {
        ScannedMarket {
            ticker: ticker.to_string(),
            title: title.to_string(),
            subtitle: None,
            market_type: None,
            event_ticker: None,
            series_ticker: None,
            yes_bid_cents: Some(45.0),
            yes_ask_cents: Some(50.0),
            volume,
            close_time: None,
        }
    }

    #[test]
    fn select_for_enrichment_round_robins_categories() {
        // 3 crypto (high volume), 1 weather, 1 sports — limit=3
        // Without balancing: all 3 slots go to crypto.
        // With balancing: Weather gets slot 1, Sports slot 2, Crypto slot 3.
        let markets = vec![
            mk_market("KXBTC-A", "Bitcoin price", 50_000.0),
            mk_market("KXBTC-B", "Bitcoin price", 40_000.0),
            mk_market("KXBTC-C", "Bitcoin price", 30_000.0),
            mk_market("KXHIGHCHI-26FEB16-T45", "Chicago high temp above 45", 5_000.0),
            mk_market("KXNBA-GAME", "NBA total points", 8_000.0),
        ];
        let selected = select_for_enrichment(&markets, 3);
        let tickers: Vec<&str> = selected.iter().map(|m| m.ticker.as_str()).collect();
        assert!(tickers.contains(&"KXHIGHCHI-26FEB16-T45"), "weather should get a slot");
        assert!(tickers.contains(&"KXNBA-GAME"), "sports should get a slot");
        assert_eq!(tickers.len(), 3);
    }

    #[test]
    fn select_for_enrichment_picks_highest_volume_within_category() {
        let markets = vec![
            mk_market("KXBTC-LOW",  "Bitcoin price", 1_000.0),
            mk_market("KXBTC-HIGH", "Bitcoin price", 9_000.0),
            mk_market("KXBTC-MID",  "Bitcoin price", 5_000.0),
        ];
        let selected = select_for_enrichment(&markets, 2);
        assert_eq!(selected[0].ticker, "KXBTC-HIGH");
        assert_eq!(selected[1].ticker, "KXBTC-MID");
    }

    #[test]
    fn select_for_enrichment_respects_limit() {
        let markets: Vec<ScannedMarket> = (0..20)
            .map(|i| mk_market(&format!("KXBTC-{i}"), "Bitcoin price", i as f64 * 1000.0))
            .collect();
        let selected = select_for_enrichment(&markets, 5);
        assert_eq!(selected.len(), 5);
    }

    #[test]
    fn extract_coin_id_maps_known_coins() {
        assert_eq!(extract_coin_id("KXBTC-26MAR2617-B63325"), Some("bitcoin"));
        assert_eq!(extract_coin_id("KXBTCD-26MAR26-B63000"), Some("bitcoin"));
        assert_eq!(extract_coin_id("KXETH-26MAR2617-B3500"), Some("ethereum"));
        assert_eq!(extract_coin_id("KXETHD-26MAR26-B3200"), Some("ethereum"));
        assert_eq!(extract_coin_id("KXSOLD-26MAR26-B155"), Some("solana"));
        assert_eq!(extract_coin_id("KXXRPD-26MAR26-B2"), Some("ripple"));
    }

    #[test]
    fn extract_coin_id_returns_none_for_non_crypto() {
        assert_eq!(extract_coin_id("KXHIGHCHI-26FEB16-T45"), None);
        assert_eq!(extract_coin_id("KXNBAGAME-26MAR24"), None);
        assert_eq!(extract_coin_id("KXPRESRACE-2024"), None);
    }

    #[test]
    fn crypto_price_signal_normalization() {
        // Simulate the signal computation: (price - threshold) / (threshold * 0.05)
        let threshold = 63_325.0_f64;
        let at_threshold = ((threshold - threshold) / (threshold * 0.05)).clamp(-1.0, 1.0);
        assert_eq!(at_threshold, 0.0);

        let five_pct_above = ((threshold * 1.05 - threshold) / (threshold * 0.05)).clamp(-1.0, 1.0);
        assert!((five_pct_above - 1.0).abs() < 1e-9, "5% above should be +1.0");

        let ten_pct_below = ((threshold * 0.90 - threshold) / (threshold * 0.05)).clamp(-1.0, 1.0);
        assert_eq!(ten_pct_below, -1.0, "10% below should clamp to -1.0");
    }
}
