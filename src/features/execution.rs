use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};

use crate::data::market_scanner::ScannedMarket;
use crate::execution::types::{Side, TimeInForce, TradeSignal};
use crate::features::forecast::{ForecastFeatureRow, build_forecast_feature_row};
use crate::model::valuation::CandidateTrade;
use crate::research::events::OrderLifecycleEvent;

pub const EXECUTION_FEATURE_SCHEMA_VERSION: &str = "v1";

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct ExecutionContext {
    pub open_order_count_same_ticker: u32,
    pub recent_fill_count_same_ticker: u32,
    pub recent_cancel_count_same_ticker: u32,
    pub same_event_exposure_notional: f64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ExecutionFeatureRow {
    pub schema_version: String,
    pub feature_ts: DateTime<Utc>,
    pub ticker: String,
    pub outcome_id: String,
    pub side: Side,
    pub tif: TimeInForce,
    pub title: String,
    pub vertical: String,
    pub candidate_limit_price: f64,
    pub candidate_observed_price: f64,
    pub candidate_fair_price: f64,
    pub raw_edge_pct: f64,
    pub confidence: f64,
    pub yes_bid_cents: Option<f64>,
    pub yes_ask_cents: Option<f64>,
    pub spread_cents: Option<f64>,
    pub mid_prob_yes: Option<f64>,
    pub volume: f64,
    pub time_to_close_secs: Option<i64>,
    pub price_vs_best_bid_cents: Option<f64>,
    pub price_vs_best_ask_cents: Option<f64>,
    pub aggressiveness_bps: Option<f64>,
    pub open_order_count_same_ticker: u32,
    pub recent_fill_count_same_ticker: u32,
    pub recent_cancel_count_same_ticker: u32,
    pub same_event_exposure_notional: f64,
}

pub fn build_execution_feature_row(
    market: &ScannedMarket,
    candidate: &CandidateTrade,
    tif: TimeInForce,
    limit_price: f64,
    context: &ExecutionContext,
    feature_ts: DateTime<Utc>,
) -> ExecutionFeatureRow {
    let forecast = build_forecast_feature_row(market, None, feature_ts);
    build_execution_feature_row_from_forecast(&forecast, market, candidate, tif, limit_price, context)
}

pub fn build_execution_feature_row_from_forecast(
    forecast: &ForecastFeatureRow,
    market: &ScannedMarket,
    candidate: &CandidateTrade,
    tif: TimeInForce,
    limit_price: f64,
    context: &ExecutionContext,
) -> ExecutionFeatureRow {
    let limit_cents = limit_price * 100.0;

    // For NO trades, the relevant market side is flipped:
    //   NO ask  = 100 - YES bid   (buying NO = selling YES)
    //   NO bid  = 100 - YES ask
    // Aggressiveness measures how far the limit price is from the side's own best ask,
    // normalized by spread. Using YES prices for NO trades produces systematically wrong
    // aggressiveness values (e.g. NO limit=1¢ vs YES ask=99¢ → appears as "deep miss"
    // when the NO ask is actually 1¢ → "at market").
    let (side_ask_cents, side_bid_cents) = if candidate.outcome_id.eq_ignore_ascii_case("no") {
        (
            market.yes_bid_cents.map(|b| 100.0 - b), // NO ask = 100 - YES bid
            market.yes_ask_cents.map(|a| 100.0 - a), // NO bid = 100 - YES ask
        )
    } else {
        (market.yes_ask_cents, market.yes_bid_cents)
    };

    let price_vs_best_bid_cents = side_bid_cents.map(|bid| limit_cents - bid);
    let price_vs_best_ask_cents = side_ask_cents.map(|ask| limit_cents - ask);
    let aggressiveness_bps = market
        .spread_cents()
        .filter(|spread| *spread > 0.0)
        .and_then(|spread| {
            price_vs_best_ask_cents
                .or(price_vs_best_bid_cents)
                .map(|offset| (offset / spread) * 10_000.0)
        });

    ExecutionFeatureRow {
        schema_version: EXECUTION_FEATURE_SCHEMA_VERSION.to_string(),
        feature_ts: forecast.feature_ts,
        ticker: candidate.ticker.clone(),
        outcome_id: candidate.outcome_id.clone(),
        side: candidate.side,
        tif,
        title: market.title.clone(),
        vertical: forecast.vertical.clone(),
        candidate_limit_price: limit_price,
        candidate_observed_price: candidate.observed_price,
        candidate_fair_price: candidate.fair_price,
        raw_edge_pct: candidate.edge_pct,
        confidence: candidate.confidence,
        yes_bid_cents: market.yes_bid_cents,
        yes_ask_cents: market.yes_ask_cents,
        spread_cents: market.spread_cents(),
        mid_prob_yes: forecast.mid_prob_yes,
        volume: market.volume,
        time_to_close_secs: forecast.time_to_close_secs,
        price_vs_best_bid_cents,
        price_vs_best_ask_cents,
        aggressiveness_bps,
        open_order_count_same_ticker: context.open_order_count_same_ticker,
        recent_fill_count_same_ticker: context.recent_fill_count_same_ticker,
        recent_cancel_count_same_ticker: context.recent_cancel_count_same_ticker,
        same_event_exposure_notional: context.same_event_exposure_notional,
    }
}

pub fn build_execution_feature_row_from_order_event(
    event: &OrderLifecycleEvent,
    market: Option<&ScannedMarket>,
    signal: Option<&TradeSignal>,
    context: &ExecutionContext,
) -> ExecutionFeatureRow {
    let yes_bid_cents = market.and_then(|m| m.yes_bid_cents);
    let yes_ask_cents = market.and_then(|m| m.yes_ask_cents);
    let spread_cents = match (yes_bid_cents, yes_ask_cents) {
        (Some(bid), Some(ask)) if ask >= bid => Some(ask - bid),
        _ => None,
    };
    let candidate_limit_price = event.limit_price.unwrap_or_else(|| signal.map(|s| s.observed_price).unwrap_or(0.5));
    let limit_cents = candidate_limit_price * 100.0;
    let price_vs_best_bid_cents = yes_bid_cents.map(|bid| limit_cents - bid);
    let price_vs_best_ask_cents = yes_ask_cents.map(|ask| limit_cents - ask);
    let aggressiveness_bps = spread_cents
        .filter(|spread| *spread > 0.0)
        .and_then(|spread| {
            price_vs_best_ask_cents
                .or(price_vs_best_bid_cents)
                .map(|offset| (offset / spread) * 10_000.0)
        });

    ExecutionFeatureRow {
        schema_version: EXECUTION_FEATURE_SCHEMA_VERSION.to_string(),
        feature_ts: event.ts,
        ticker: event.ticker.clone(),
        outcome_id: event.outcome_id.clone(),
        side: event.side,
        tif: event.tif,
        title: market.map(|m| m.title.clone()).unwrap_or_default(),
        vertical: "unknown".to_string(),
        candidate_limit_price,
        candidate_observed_price: signal.map(|s| s.observed_price).unwrap_or(candidate_limit_price),
        candidate_fair_price: signal.map(|s| s.fair_price).unwrap_or(candidate_limit_price),
        raw_edge_pct: signal.map(|s| s.edge_pct).unwrap_or(0.0),
        confidence: signal.map(|s| s.confidence).unwrap_or(0.0),
        yes_bid_cents,
        yes_ask_cents,
        spread_cents,
        mid_prob_yes: match (yes_bid_cents, yes_ask_cents) {
            (Some(bid), Some(ask)) => Some(((bid + ask) / 2.0 / 100.0).clamp(0.0, 1.0)),
            _ => None,
        },
        volume: market.map(|m| m.volume).unwrap_or(0.0),
        time_to_close_secs: market
            .and_then(|m| m.close_time)
            .map(|close| (close - event.ts).num_seconds()),
        price_vs_best_bid_cents,
        price_vs_best_ask_cents,
        aggressiveness_bps,
        open_order_count_same_ticker: context.open_order_count_same_ticker,
        recent_fill_count_same_ticker: context.recent_fill_count_same_ticker,
        recent_cancel_count_same_ticker: context.recent_cancel_count_same_ticker,
        same_event_exposure_notional: context.same_event_exposure_notional,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn market() -> ScannedMarket {
        ScannedMarket {
            ticker: "KXHIGHCHI-26FEB16-T45".to_string(),
            title: "Will the high temp in Chicago be >45° on Feb 16, 2026?".to_string(),
            subtitle: None,
            market_type: Some("binary".to_string()),
            event_ticker: None,
            series_ticker: None,
            yes_bid_cents: Some(68.0),
            yes_ask_cents: Some(72.0),
            volume: 2000.0,
            close_time: None,
        }
    }

    fn candidate() -> CandidateTrade {
        CandidateTrade {
            ticker: "KXHIGHCHI-26FEB16-T45".to_string(),
            side: Side::Buy,
            outcome_id: "yes".to_string(),
            fair_price: 0.78,
            observed_price: 0.70,
            edge_pct: 0.06,
            confidence: 0.8,
            rationale: "x".to_string(),
        }
    }

    #[test]
    fn computes_execution_offsets() {
        let row = build_execution_feature_row(
            &market(),
            &candidate(),
            TimeInForce::Gtc,
            0.71,
            &ExecutionContext {
                open_order_count_same_ticker: 1,
                recent_fill_count_same_ticker: 2,
                recent_cancel_count_same_ticker: 0,
                same_event_exposure_notional: 150.0,
            },
            Utc::now(),
        );
        assert_eq!(row.price_vs_best_bid_cents, Some(3.0));
        assert_eq!(row.price_vs_best_ask_cents, Some(-1.0));
    }

    #[test]
    fn no_trade_aggressiveness_uses_no_side_prices() {
        // YES bid=99, YES ask=100 → market near-resolved YES.
        // NO ask = 100 - 99 = 1¢. Buying NO at 1¢ is "at market", not "deep_miss".
        // The old code used YES prices: limit=1 vs YES ask=100 → -99¢ offset → deep_miss.
        // The new code uses NO prices: limit=1 vs NO ask=1 → 0¢ offset → at_market.
        let near_resolved = ScannedMarket {
            ticker: "KXNBATOTAL-26MAR25LALIND-234".to_string(),
            title: "NBA Total".to_string(),
            subtitle: None,
            market_type: None,
            event_ticker: None,
            series_ticker: None,
            yes_bid_cents: Some(99.0),
            yes_ask_cents: Some(100.0),
            volume: 5000.0,
            close_time: None,
        };
        let no_candidate = CandidateTrade {
            ticker: "KXNBATOTAL-26MAR25LALIND-234".to_string(),
            side: Side::Buy,
            outcome_id: "no".to_string(),
            fair_price: 0.42,
            observed_price: 0.01,
            edge_pct: 0.41,
            confidence: 0.6,
            rationale: "x".to_string(),
        };
        let row = build_execution_feature_row(
            &near_resolved,
            &no_candidate,
            TimeInForce::Ioc,
            0.01,  // limit = NO ask = 1¢
            &ExecutionContext::default(),
            Utc::now(),
        );
        // NO ask = 100 - YES bid = 100 - 99 = 1¢; limit = 1¢ → offset = 0
        assert_eq!(row.price_vs_best_ask_cents, Some(0.0),
            "NO limit at NO ask should have 0 offset, got {:?}", row.price_vs_best_ask_cents);
        // NO bid = 100 - YES ask = 100 - 100 = 0¢; limit = 1¢ → offset = +1
        assert_eq!(row.price_vs_best_bid_cents, Some(1.0));
    }

    #[test]
    fn yes_trade_aggressiveness_unchanged() {
        // YES side should still use YES prices directly.
        let m = market(); // bid=68, ask=72
        let c = candidate(); // outcome_id=yes, limit=0.70
        let row = build_execution_feature_row(&m, &c, TimeInForce::Gtc, 0.70,
            &ExecutionContext::default(), Utc::now());
        // YES side: ask=72, limit=70 → offset = 70 - 72 = -2 (below ask)
        assert_eq!(row.price_vs_best_ask_cents, Some(-2.0));
        assert_eq!(row.price_vs_best_bid_cents, Some(2.0));
    }
}
