use school_core::{context, model, strict_json};
use serde_json::{json, Value};

fn call(id: &str, arguments: &str) -> Value {
    json!({"id": id, "type": "function", "function": {"name": "submit", "arguments": arguments}})
}
#[test]
fn strict_arguments_reject_ambiguity_before_any_call() {
    for raw in [
        r#"{"a":1,"a":2}"#,
        r#"{"a":{"b":1,"b":2}}"#,
        r#"{"a":NaN}"#,
        r#"{"a":1e999}"#,
        r#"{"a":18446744073709551616}"#,
        "[]",
        "null",
        "{} trailing",
    ] {
        assert_eq!(
            model::tool_arguments(&json!(raw)).unwrap_err().code,
            "invalid_tool_arguments"
        );
    }
    assert_eq!(
        model::tool_arguments(&json!(r#"{"a":9007199254740993,"b":false}"#)).unwrap()["a"],
        9007199254740993_u64
    );
    assert!(strict_json::parse(br#"{"a":1,"\u0061":2}"#).is_err());
    let deep = format!("{}0{}", "[".repeat(200), "]".repeat(200));
    assert!(strict_json::parse(deep.as_bytes()).is_err());
}
#[test]
fn malformed_later_call_rejects_entire_batch() {
    let value =
        json!({"role":"assistant", "tool_calls":[call("a", "{}"),call("b", "{\"x\":1,\"x\":2}")]});
    assert_eq!(
        model::validated_message(value).unwrap_err().code,
        "invalid_tool_arguments"
    );
    let value = json!({"role":"assistant", "tool_calls":[call("a", "{}"),call("a", "{}")]});
    assert_eq!(
        model::validated_message(value).unwrap_err().code,
        "invalid_model_response"
    );
}
#[test]
fn message_filters_unknown_fields_but_preserves_reasoning() {
    let value = json!({"role":"assistant", "content":"done", "reasoning_content":"analysis", "secret":"drop"});
    let result = model::validated_message(value).unwrap();
    assert_eq!(result["reasoning_content"], "analysis");
    assert!(result.get("secret").is_none());
    assert_eq!(
        model::validated_message(json!({"role":"assistant", "content":" "}))
            .unwrap_err()
            .code,
        "empty_response"
    );
    assert_eq!(
        model::validated_message(json!({"role":"assistant", "refusal":"no"}))
            .unwrap_err()
            .code,
        "model_refusal"
    );
}
#[test]
fn configuration_cannot_smuggle_credentials_or_remote_http() {
    for url in [
        "http://example.org",
        "https://user:pass@example.org",
        "https://example.org?token=x",
        "https://example.org#x",
        "http://127.1",
        "http://2130706433",
        "http://127.0.0.1:0",
        "file:///test",
    ] {
        assert!(model::normalize_url(url).is_err(), "{url}");
    }
    for url in [
        "http://localhost:8000/v1",
        "http://127.0.0.1:8080",
        "http://[::1]:8000",
        "https://example.org/v1",
    ] {
        assert!(model::normalize_url(url).is_ok(), "{url}");
    }
    assert_eq!(
        model::normalize_url(" https://example.org/v1/chat/completions/ ").unwrap(),
        "https://example.org/v1"
    );
    assert!(model::Settings::parse(
        &json!({"base_url":"https://example.org", "model":"m", "api_key":"key\nvalue"})
    )
    .is_err());
}
#[test]
fn projection_keeps_user_corrections_receipts_and_complete_batches() {
    let input = json!({"messages": [
        {"role":"user","content":"先准备，不提交"},
        {"role":"assistant","tool_calls":[call("a", "{}")]},
        {"role":"tool","tool_call_id":"a","content":"large result".repeat(1000)},
        {"role":"assistant","content":"old explanation"},
        {"role":"user","content":"最终更正：目标保持不变"}],
        "instructions":"test", "tools":[], "model":"m", "budget":1000,
        "memory":{"operations":[{"state":"unknown", "receipt":"exact", "authorized":false}]}});
    let result = context::project(&input).unwrap();
    assert_eq!(result["report"]["compacted"], true);
    let messages = result["messages"].as_array().unwrap();
    let users: Vec<_> = messages.iter().filter(|m| m["role"] == "user").collect();
    assert_eq!(
        users,
        input["messages"]
            .as_array()
            .unwrap()
            .iter()
            .filter(|m| m["role"] == "user")
            .collect::<Vec<_>>()
    );
    assert!(messages[1]["content"].as_str().unwrap().contains("exact"));
    context::units(messages).unwrap();
    assert!(result["report"]["request_bytes"].as_u64().unwrap() <= 1000);
}
#[test]
fn insufficient_budget_and_incomplete_batches_fail_closed() {
    let mut input = json!({"messages":[{"role":"user","content":"x".repeat(2000)}], "instructions":"s",
        "tools":[], "model":"m", "budget":500, "memory":{}});
    assert_eq!(
        context::project(&input).unwrap_err().code,
        "context_capacity"
    );
    input["messages"] = json!([{"role":"assistant","tool_calls":[call("a", "{}")]}]);
    assert_eq!(
        context::project(&input).unwrap_err().code,
        "context_incomplete_batch"
    );
    input["messages"] = json!([{"role":"assistant","tool_calls":[call("a", "{}")]},
        {"role":"tool","tool_call_id":"a","content":"1"},{"role":"tool","tool_call_id":"a","content":"2"}]);
    assert_eq!(
        context::project(&input).unwrap_err().code,
        "context_incomplete_batch"
    );
}
