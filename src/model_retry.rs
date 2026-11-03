//! Retry transient provider failures without repeating any tool side effects.
use std::time::Duration;
use crate::model::ModelError;

pub const MAX_ATTEMPTS: u32 = 12;
pub const MAX_RETRY_WINDOW: Duration = Duration::from_secs(20 * 60);

pub fn delay(error: &ModelError, attempt: u32, jitter: u64) -> Option<Duration> {
    let exponential = (2u64.saturating_pow(attempt.min(6))).min(60) + jitter % 5;
    let seconds = match error {
        ModelError::Server { status, .. } if (500..600).contains(status) || *status == 408 => exponential,
        ModelError::Timeout { .. } | ModelError::Transport { .. } => exponential,
        ModelError::RateLimited { retry_after_secs, .. } => retry_after_secs.unwrap_or(exponential).max(exponential),
        _ => return None,
    };
    Some(Duration::from_secs(seconds))
}

pub fn run<T>(
    mut request: impl FnMut(Duration) -> Result<T, ModelError>,
    mut remaining: impl FnMut() -> Duration,
    mut wait: impl FnMut(Duration),
    mut observe: impl FnMut(&ModelError, u32, Duration),
    max_attempts: u32,
    jitter: u64,
) -> Result<T, ModelError> {
    for attempt in 1..=max_attempts.max(1) {
        let budget = remaining();
        if budget.is_zero() { return Err(ModelError::Timeout { secs: 0 }); }
        match request(budget) {
            Ok(value) => return Ok(value),
            Err(error) => {
                let Some(delay) = delay(&error, attempt, jitter + u64::from(attempt)) else { return Err(error); };
                if attempt >= max_attempts || remaining() <= delay + Duration::from_secs(1) {
                    return Err(error);
                }
                observe(&error, attempt, delay);
                wait(delay);
            }
        }
    }
    unreachable!()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{credentials::Secret, model::{self, Message, ModelConfig}, test_stub::{Stub, StubReply}};

    #[test]
    fn repeated_502_recovers_with_the_same_context_and_key_stays_out_of_records() {
        let completion = r#"{"choices":[{"message":{"role":"assistant","content":"recovered"},"finish_reason":"stop"}]}"#;
        let stub = Stub::start(vec![StubReply::status(502,"bad gateway"), StubReply::status(502,"bad gateway"), StubReply::json(completion)]);
        let config = ModelConfig { endpoint:stub.url(), model:"fixture".into(), api_key:Secret::new("fixture-key"), timeout:Duration::from_secs(3) };
        let mut delays = Vec::new();
        let result = run(|_| model::chat(&config, &[Message::user("continue previous work")], &[], Some(32)),
            || Duration::from_secs(60), |_| {}, |e,n,d| { assert_eq!(e.class(),"server_error"); delays.push((n,d)); }, 12, 0).unwrap();
        assert_eq!(result.reply.content.as_deref(),Some("recovered"));
        assert_eq!(delays.len(),2);
        let requests = stub.requests();
        assert_eq!(requests.len(),3);
        assert_eq!(requests[0].body,requests[2].body);
    }

    #[test]
    fn auth_errors_are_not_retried_and_persistent_502_is_bounded() {
        let mut calls=0;
        let result: Result<(),_> = run(|_| {calls+=1;Err(ModelError::BadKey{status:401})}, ||Duration::from_secs(600), |_|panic!("no sleep"), |_,_,_|panic!("no retry"),12,0);
        assert!(result.is_err()); assert_eq!(calls,1);
        calls=0;
        let result: Result<(),_> = run(|_| {calls+=1;Err(ModelError::Server{status:502,message:String::new()})}, ||Duration::from_secs(600), |_|{}, |_,_,_|{},12,0);
        assert!(result.is_err()); assert_eq!(calls,12);
    }

    #[test]
    fn retry_after_and_session_deadline_are_respected() {
        let error=ModelError::RateLimited{status:429,retry_after_secs:Some(180)};
        assert_eq!(delay(&error,1,0),Some(Duration::from_secs(180)));
        let mut calls=0;
        let result:Result<(),_>=run(|_| {calls+=1;Err(ModelError::Timeout{secs:1})},||Duration::from_secs(1),|_|panic!("deadline"),|_,_,_|panic!("deadline"),12,0);
        assert!(result.is_err()); assert_eq!(calls,1);
        assert!(delay(&ModelError::Server{status:400,message:String::new()},1,0).is_none());
    }
}
