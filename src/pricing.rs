//! Price table and cost estimation. Costs are API list-price estimates, not invoices.
//! 단가표와 비용 추정입니다. 비용은 API 공시가로 환산한 추정치이며 청구서가 아닙니다.

use serde::{Deserialize, Serialize};
use std::collections::HashMap;

/// Pricing modifier bits stored per event row.
/// 이벤트 행마다 저장하는 가격 변경 요인 비트입니다.
pub const MOD_FAST: u8 = 1;
pub const MOD_BATCH: u8 = 2;
pub const MOD_US_INFERENCE: u8 = 4;

/// The price table compiled into the binary.
/// 바이너리에 포함되는 기본 단가표입니다.
pub const EMBEDDED_PRICES: &str = include_str!("../prices.json");

/// USD per million tokens for each token kind.
/// 토큰 종류별 백만 토큰당 미국 달러입니다.
#[derive(Debug, Clone, Copy, PartialEq, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Rates {
    pub input: f64,
    pub output: f64,
    pub cache_write_5m: f64,
    pub cache_write_1h: f64,
    pub cache_read: f64,
}

/// One price entry shared by one or more model ids.
/// 하나 이상의 모델 id가 함께 쓰는 단가 항목입니다.
#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct PriceEntry {
    pub models: Vec<String>,
    pub rates: Rates,
    /// Rates for a request whose prompt is longer than the table's threshold.
    /// 프롬프트가 단가표의 기준 길이를 넘는 요청에 적용하는 단가입니다.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub long_prompt_rates: Option<Rates>,
    /// Multiplier for fast mode. Absent means the model has no fast-mode price.
    /// 빠른 모드 배수입니다. 없으면 그 모델에 빠른 모드 가격이 없다는 뜻입니다.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub fast_multiplier: Option<f64>,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct PriceTable {
    pub as_of: String,
    #[serde(default)]
    pub source: String,
    #[serde(default)]
    pub notes: Vec<String>,
    pub long_prompt_threshold_tokens: i64,
    pub batch_multiplier: f64,
    pub us_inference_multiplier: f64,
    pub web_search_usd_per_request: f64,
    pub models: Vec<PriceEntry>,
}

/// Token counts of one aggregated group that shares a model and pricing modifiers.
/// 모델과 가격 변경 요인이 같은 집계 묶음 하나의 토큰 수입니다.
#[derive(Debug, Clone, Copy, Default, PartialEq)]
pub struct TokenCounts {
    pub input: i64,
    pub output: i64,
    pub cache_write_5m: i64,
    pub cache_write_1h: i64,
    pub cache_read: i64,
    pub web_search_requests: i64,
}

/// Estimated cost in USD split by what was billed.
/// 과금 항목별로 나눈 추정 비용(미국 달러)입니다.
#[derive(Debug, Clone, Copy, Default, PartialEq, Serialize)]
pub struct CostBreakdown {
    pub input: f64,
    pub output: f64,
    pub cache_write: f64,
    pub cache_read: f64,
    pub web_search: f64,
}

impl CostBreakdown {
    pub fn total(&self) -> f64 {
        self.input + self.output + self.cache_write + self.cache_read + self.web_search
    }

    pub fn add(&mut self, other: &CostBreakdown) {
        self.input += other.input;
        self.output += other.output;
        self.cache_write += other.cache_write;
        self.cache_read += other.cache_read;
        self.web_search += other.web_search;
    }
}

#[derive(Debug, Clone)]
pub struct Pricing {
    table: PriceTable,
    index: HashMap<String, usize>,
}

impl Pricing {
    /// Load the table compiled into the binary.
    /// 바이너리에 포함된 단가표를 불러옵니다.
    pub fn embedded() -> Pricing {
        Pricing::from_json(EMBEDDED_PRICES).expect("embedded prices.json must be valid")
    }

    pub fn from_json(text: &str) -> Result<Pricing, String> {
        let table: PriceTable =
            serde_json::from_str(text).map_err(|e| format!("invalid price table: {e}"))?;
        let mut index = HashMap::new();
        check_number("batch_multiplier", table.batch_multiplier)?;
        check_number("us_inference_multiplier", table.us_inference_multiplier)?;
        check_number(
            "web_search_usd_per_request",
            table.web_search_usd_per_request,
        )?;
        if table.long_prompt_threshold_tokens <= 0 {
            return Err(
                "invalid price table: long_prompt_threshold_tokens must be positive".into(),
            );
        }
        for (i, entry) in table.models.iter().enumerate() {
            if entry.models.is_empty() {
                return Err(format!("invalid price table: entry {i} lists no models"));
            }
            check_rates(&entry.models[0], &entry.rates)?;
            if let Some(long) = &entry.long_prompt_rates {
                check_rates(&entry.models[0], long)?;
            }
            if let Some(fast) = entry.fast_multiplier {
                check_number("fast_multiplier", fast)?;
            }
            for model in &entry.models {
                if index.insert(model.clone(), i).is_some() {
                    return Err(format!(
                        "invalid price table: model '{model}' is listed twice"
                    ));
                }
            }
        }
        Ok(Pricing { table, index })
    }

    pub fn table(&self) -> &PriceTable {
        &self.table
    }

    pub fn long_prompt_threshold(&self) -> i64 {
        self.table.long_prompt_threshold_tokens
    }

    /// Find the price entry for a model id. A dated snapshot id such as
    /// "claude-haiku-4-5-20251001" resolves to its undated id. Nothing else is guessed, so an
    /// unknown model stays unpriced instead of borrowing a neighbour's price.
    /// 모델 id의 단가 항목을 찾습니다. "claude-haiku-4-5-20251001" 같은 날짜 스냅샷 id는
    /// 날짜 없는 id로 찾습니다. 그 밖의 추측은 하지 않으므로, 모르는 모델은 비슷한 모델의
    /// 단가를 빌리지 않고 단가 없음으로 남습니다.
    pub fn resolve(&self, model: &str) -> Option<&PriceEntry> {
        let i = self
            .index
            .get(model)
            .or_else(|| self.index.get(strip_date_suffix(model)))?;
        self.table.models.get(*i)
    }

    /// Estimate the cost of a group of requests. Returns None when the model has no price.
    /// 요청 묶음의 비용을 추정합니다. 모델에 단가가 없으면 None을 돌려줍니다.
    pub fn cost(
        &self,
        model: &str,
        mods: u8,
        long_prompt: bool,
        tokens: &TokenCounts,
    ) -> Option<CostBreakdown> {
        let entry = self.resolve(model)?;
        let rates = match (&entry.long_prompt_rates, long_prompt) {
            (Some(long), true) => long,
            _ => &entry.rates,
        };
        let mut multiplier = 1.0;
        if mods & MOD_FAST != 0 {
            multiplier *= entry.fast_multiplier.unwrap_or(1.0);
        }
        if mods & MOD_BATCH != 0 {
            multiplier *= self.table.batch_multiplier;
        }
        if mods & MOD_US_INFERENCE != 0 {
            multiplier *= self.table.us_inference_multiplier;
        }
        let per_token =
            |count: i64, usd_per_mtok: f64| count as f64 * usd_per_mtok / 1e6 * multiplier;
        Some(CostBreakdown {
            input: per_token(tokens.input, rates.input),
            output: per_token(tokens.output, rates.output),
            cache_write: per_token(tokens.cache_write_5m, rates.cache_write_5m)
                + per_token(tokens.cache_write_1h, rates.cache_write_1h),
            cache_read: per_token(tokens.cache_read, rates.cache_read),
            // Web search is billed per request and is not scaled by token modifiers.
            // 웹 검색은 요청 수로 과금되며 토큰 가격 변경 요인의 영향을 받지 않습니다.
            web_search: tokens.web_search_requests as f64 * self.table.web_search_usd_per_request,
        })
    }
}

fn check_number(name: &str, value: f64) -> Result<(), String> {
    if value.is_finite() && value >= 0.0 {
        Ok(())
    } else {
        Err(format!(
            "invalid price table: {name} must be a non-negative number"
        ))
    }
}

fn check_rates(model: &str, rates: &Rates) -> Result<(), String> {
    let all = [
        rates.input,
        rates.output,
        rates.cache_write_5m,
        rates.cache_write_1h,
        rates.cache_read,
    ];
    if all.iter().all(|v| v.is_finite() && *v >= 0.0) {
        Ok(())
    } else {
        Err(format!(
            "invalid price table: rates for '{model}' must be non-negative numbers"
        ))
    }
}

/// Drop a trailing "-YYYYMMDD" snapshot date from a model id.
/// 모델 id 끝의 "-YYYYMMDD" 스냅샷 날짜를 떼어 냅니다.
fn strip_date_suffix(model: &str) -> &str {
    let bytes = model.as_bytes();
    if bytes.len() > 9 {
        let (head, tail) = bytes.split_at(bytes.len() - 9);
        if tail[0] == b'-' && tail[1..].iter().all(u8::is_ascii_digit) {
            return &model[..head.len()];
        }
    }
    model
}

#[cfg(test)]
mod tests {
    use super::*;

    fn close(a: f64, b: f64) -> bool {
        (a - b).abs() < 1e-9
    }

    #[test]
    fn embedded_table_loads_and_resolves_models() {
        let p = Pricing::embedded();
        assert!(p.resolve("claude-fable-5-2").is_some());
        assert!(p.resolve("claude-haiku-4-5-20251001").is_some());
        // An unknown newer model must not borrow the price of a similarly named one.
        // 모르는 새 모델이 이름이 비슷한 모델의 단가를 빌려서는 안 됩니다.
        assert!(p.resolve("claude-fable-5-3").is_none());
        assert!(p.resolve("gpt-6-astra").is_none());
        assert!(p.resolve("<synthetic>").is_none());
    }

    #[test]
    fn reconciles_with_costs_recorded_by_claude_code() {
        // Token counts and costUSD copied from Claude Code session cost records.
        // Claude Code 세션 비용 기록에서 가져온 토큰 수와 costUSD입니다.
        let p = Pricing::embedded();
        let fable_5_1 = TokenCounts {
            input: 8,
            output: 1623,
            cache_write_1h: 43829,
            cache_read: 119546,
            ..Default::default()
        };
        let cost = p.cost("claude-fable-5-1", 0, false, &fable_5_1).unwrap();
        assert!(close(cost.total(), 0.9876965), "{}", cost.total());

        let fable_5 = TokenCounts {
            input: 122,
            output: 99593,
            cache_write_1h: 215359,
            cache_read: 9578424,
            ..Default::default()
        };
        let cost = p.cost("claude-fable-5", 0, false, &fable_5).unwrap();
        assert!(close(cost.total(), 18.866474), "{}", cost.total());

        let haiku = TokenCounts {
            input: 1844,
            output: 32,
            ..Default::default()
        };
        let cost = p
            .cost("claude-haiku-4-5-20251001", 0, false, &haiku)
            .unwrap();
        assert!(close(cost.total(), 0.002004), "{}", cost.total());
    }

    #[test]
    fn applies_modifiers_and_long_prompt_rates() {
        let p = Pricing::embedded();
        let t = TokenCounts {
            input: 1_000_000,
            output: 1_000_000,
            ..Default::default()
        };
        let standard = p.cost("claude-fable-5-2", 0, false, &t).unwrap().total();
        assert!(close(standard, 48.0));
        assert!(close(
            p.cost("claude-fable-5-2", MOD_FAST, false, &t)
                .unwrap()
                .total(),
            96.0
        ));
        assert!(close(
            p.cost("claude-fable-5-2", MOD_BATCH, false, &t)
                .unwrap()
                .total(),
            24.0
        ));
        assert!(close(
            p.cost("claude-fable-5-2", MOD_US_INFERENCE, false, &t)
                .unwrap()
                .total(),
            52.8
        ));
        // A model without a fast price is charged the standard rate.
        // 빠른 모드 가격이 없는 모델은 표준 단가로 계산합니다.
        assert!(close(
            p.cost("claude-fable-5-1", MOD_FAST, false, &t)
                .unwrap()
                .total(),
            60.0
        ));
        // Only Haiku 5.5 has long-prompt rates.
        // 긴 프롬프트 단가는 Haiku 5.5에만 있습니다.
        assert!(close(
            p.cost("claude-haiku-5-5", 0, false, &t).unwrap().total(),
            0.6
        ));
        assert!(close(
            p.cost("claude-haiku-5-5", 0, true, &t).unwrap().total(),
            3.0
        ));
        assert!(close(
            p.cost("claude-fable-5-2", 0, true, &t).unwrap().total(),
            48.0
        ));
    }

    #[test]
    fn web_search_is_billed_per_request() {
        let p = Pricing::embedded();
        let t = TokenCounts {
            web_search_requests: 3,
            ..Default::default()
        };
        let cost = p.cost("claude-opus-5-5", MOD_FAST, false, &t).unwrap();
        assert!(close(cost.web_search, 0.03));
        assert!(close(cost.total(), 0.03));
    }

    #[test]
    fn rejects_bad_tables() {
        assert!(Pricing::from_json("{}").is_err());
        let duplicate = r#"{"as_of":"x","long_prompt_threshold_tokens":1,"batch_multiplier":0.5,
            "us_inference_multiplier":1.1,"web_search_usd_per_request":0.01,"models":[
            {"models":["a","a"],"rates":{"input":1,"output":1,"cache_write_5m":1,"cache_write_1h":1,"cache_read":1}}]}"#;
        assert!(
            Pricing::from_json(duplicate)
                .unwrap_err()
                .contains("listed twice")
        );
        let negative = duplicate
            .replace("[\"a\",\"a\"]", "[\"a\"]")
            .replace("\"input\":1", "\"input\":-1");
        assert!(Pricing::from_json(&negative).is_err());
    }

    #[test]
    fn strips_only_date_suffixes() {
        assert_eq!(
            strip_date_suffix("claude-haiku-4-5-20251001"),
            "claude-haiku-4-5"
        );
        assert_eq!(strip_date_suffix("claude-fable-5-2"), "claude-fable-5-2");
        assert_eq!(strip_date_suffix("-20251001"), "-20251001");
        assert_eq!(strip_date_suffix("모델-20251001"), "모델");
    }
}
