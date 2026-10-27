use crate::{strict_json, Error, Result};
use serde::{Deserialize, Serialize};
use serde_json::{json, Map, Value};
use std::{collections::HashSet, time::{Duration, Instant}};

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Settings {
    pub base_url: String,
    pub model: String,
    pub api_key: String,
}
impl Settings {
    pub fn parse(value: &Value) -> Result<Self> {
        let mut settings: Self =
            serde_json::from_value(value.clone()).map_err(|_| Error::invalid())?;
        settings.base_url = normalize_url(&settings.base_url)?;
        for (value, max) in [(&mut settings.model, 160), (&mut settings.api_key, 4096)] {
            if value.chars().count() > max
                || value.contains(['\r', '\n', '\0'])
                || value.trim().is_empty()
            {
                return Err(Error::new(
                    "invalid_arguments",
                    "模型名和密钥不可为空、过长或包含换行。",
                ));
            }
            *value = value.trim().into();
        }
        Ok(settings)
    }
}
pub fn normalize_url(raw: &str) -> Result<String> {
    let mut value = raw.trim().trim_end_matches('/').to_owned();
    let error = || {
        Error::new(
            "invalid_arguments",
            "请填写 HTTPS API 基础地址；本机模型可使用 localhost 的 HTTP 地址。",
        )
    };
    if !(8..=500).contains(&raw.chars().count())
        || value.chars().any(char::is_control)
        || value.contains('\\')
    {
        return Err(error());
    }
    let url = url::Url::parse(&value).map_err(|_| error())?;
    if url.host_str().is_none()
        || !url.username().is_empty()
        || url.password().is_some()
        || url.query().is_some()
        || url.fragment().is_some()
        || url.port() == Some(0)
        || !matches!(url.scheme(), "http" | "https")
        || url.scheme() == "http"
            && !matches!(url.host_str(), Some("127.0.0.1" | "localhost" | "[::1]"))
    {
        return Err(error());
    }
    // Do not silently normalize an ambiguous HTTP IP to loopback (127.1, integer IP).
    if url.scheme() == "http" {
        let authority = value
            .split_once("://")
            .ok_or_else(error)?
            .1
            .split('/')
            .next()
            .unwrap_or("");
        let host = if authority.starts_with('[') {
            authority.split(']').next().unwrap_or("").to_owned() + "]"
        } else {
            authority
                .split(':')
                .next()
                .unwrap_or("")
                .to_ascii_lowercase()
        };
        if !matches!(host.as_str(), "127.0.0.1" | "localhost" | "[::1]") {
            return Err(error());
        }
    }
    if value.ends_with("/chat/completions") {
        value.truncate(value.len() - "/chat/completions".len());
    }
    Ok(value)
}

pub fn tool_arguments(raw: &Value) -> Result<Value> {
    let error = || {
        Error::new(
            "invalid_tool_arguments",
            "模型工具参数无效；本轮未执行这些调用，请重新描述需求。",
        )
    };
    let raw = raw.as_str().ok_or_else(error)?;
    if raw.len() > 128 * 1024 {
        return Err(error());
    }
    let result = strict_json::parse(raw.as_bytes()).map_err(|_| error())?;
    if !result.is_object() {
        return Err(error());
    }
    Ok(result)
}
fn identifier(value: &Value, max: usize) -> Option<&str> {
    value.as_str().filter(|v| {
        !v.is_empty()
            && v.len() <= max
            && v.bytes()
                .all(|c| c.is_ascii_alphanumeric() || c == b'_' || c == b'-')
    })
}
fn truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(v) => *v,
        Value::String(v) => !v.is_empty(),
        Value::Array(v) => !v.is_empty(),
        Value::Object(v) => !v.is_empty(),
        Value::Number(n) => n.as_f64() != Some(0.),
    }
}
fn response_error() -> Error {
    Error::new(
        "invalid_model_response",
        "模型响应结构不完整；本轮未执行这些调用，请检查模型兼容性。",
    )
}
pub fn validated_message(value: Value) -> Result<Value> {
    let object = value.as_object().ok_or_else(response_error)?;
    if value["role"] != "assistant" {
        return Err(response_error());
    }
    if truthy(&value["refusal"]) {
        return Err(Error::new(
            "model_refusal",
            "模型拒绝了本次请求；未执行新的工具调用。",
        ));
    }
    for key in ["content", "reasoning_content"] {
        if !value[key].is_null() && !value[key].is_string() {
            return Err(response_error());
        }
    }
    let calls = match value.get("tool_calls") {
        None | Some(Value::Null) => &[][..],
        Some(Value::Array(a)) if a.len() <= 6 => a.as_slice(),
        _ => return Err(response_error()),
    };
    if calls.is_empty() && value["content"].as_str().unwrap_or("").trim().is_empty() {
        return Err(Error::new(
            "empty_response",
            "模型返回了空结果；本轮已停止。",
        ));
    }
    let mut seen = HashSet::new();
    let mut clean_calls = Vec::new();
    for call in calls {
        let id = identifier(&call["id"], 160).ok_or_else(response_error)?;
        let name = identifier(&call["function"]["name"], 128).ok_or_else(response_error)?;
        if call["type"] != "function" || !seen.insert(id) {
            return Err(response_error());
        }
        tool_arguments(&call["function"]["arguments"])?;
        clean_calls.push(json!({"id": id, "type": "function", "function": {
            "name": name, "arguments": call["function"]["arguments"]}}));
    }
    let mut result = Map::new();
    for key in ["role", "content", "reasoning_content"] {
        if let Some(v) = object.get(key) {
            result.insert(key.into(), v.clone());
        }
    }
    if !clean_calls.is_empty() {
        result.insert("tool_calls".into(), clean_calls.into());
    }
    Ok(result.into())
}

pub fn payload(
    model: &str,
    messages: &Value,
    tools: &Value,
    force: Option<&str>,
) -> Result<Vec<u8>> {
    if !messages.is_array() || !tools.is_array() {
        return Err(Error::invalid());
    }
    let mut value = json!({"model": model, "messages": messages, "stream": false});
    if tools.as_array().is_some_and(|a| !a.is_empty()) {
        value["tools"] = tools.clone();
        value["tool_choice"] = match force {
            Some(name) => json!({"type": "function", "function": {"name": name}}),
            None => "auto".into(),
        };
    }
    serde_json::to_vec(&value).map_err(|_| Error::invalid())
}
fn http_failure(status: u16) -> Error {
    let (code, message) = match status {
        400 => (
            "model_incompatible",
            "模型接口未接受请求格式，请选择支持工具调用的兼容模型。",
        ),
        401 => (
            "model_authentication",
            "API 密钥未被接受，请在模型设置中更换。",
        ),
        402 => (
            "model_balance",
            "模型账户余额不足，请到服务商平台检查额度。",
        ),
        403 => ("model_forbidden", "API 服务拒绝访问，请检查密钥权限。"),
        429 => ("model_rate_limited", "模型服务限流或额度不足，请稍后再试。"),
        404 => (
            "model_not_found",
            "API 地址或模型不存在，请检查地址或获取模型列表。",
        ),
        _ => ("model_http_error", "模型服务返回错误状态；本轮已停止。"),
    };
    Error::new(code, message)
}
fn network_error(error: reqwest::Error) -> Error {
    if error.is_timeout() {
        Error::new("model_timeout", "模型响应超时；不会自动重复请求。")
    } else {
        Error::new("model_connection", "无法连接模型 API，请检查地址及网络。")
    }
}
pub struct Client {
    http: reqwest::Client,
}

/// Assemble SSE bytes before decoding UTF-8. Partial tool arguments are never
/// returned to the runner. Only answer text is published, never reasoning.
#[derive(Default)]
struct CompletionStream {
    buffer: Vec<u8>, event: Vec<String>, total: usize,
    message: Value, finish_reason: Option<String>, done: bool,
}
impl CompletionStream {
    fn feed(&mut self, bytes: &[u8], publish: &mut dyn FnMut(&str)) -> Result<()> {
        self.total += bytes.len();
        if self.total > 2 * 1024 * 1024 { return Err(response_error()); }
        self.buffer.extend_from_slice(bytes);
        while let Some(end) = self.buffer.iter().position(|b| *b == b'\n') {
            let raw: Vec<_> = self.buffer.drain(..=end).collect();
            let line = std::str::from_utf8(&raw[..raw.len()-1]).map_err(|_| response_error())?.trim_end_matches('\r');
            if line.is_empty() {
                if !self.event.is_empty() {
                    let data = self.event.join("\n"); self.event.clear();
                    self.event(&data, publish)?;
                }
            } else if let Some(data) = line.strip_prefix("data:") {
                self.event.push(data.strip_prefix(' ').unwrap_or(data).to_owned());
            }
            if self.done { break; }
        }
        Ok(())
    }
    fn event(&mut self, data: &str, publish: &mut dyn FnMut(&str)) -> Result<()> {
        if data == "[DONE]" { self.done = true; return Ok(()); }
        let value = strict_json::parse(data.as_bytes()).map_err(|_| response_error())?;
        if value.get("error").is_some() { return Err(response_error()); }
        let choices = value["choices"].as_array().ok_or_else(response_error)?;
        if choices.is_empty() { return Ok(()); } // Usage-only trailer.
        if choices.len()!=1 || choices[0]["index"] != 0 { return Err(response_error()); }
        let choice = &choices[0];
        if self.finish_reason.is_some() { return Err(response_error()); }
        let delta = choice["delta"].as_object().ok_or_else(response_error)?;
        if self.message.is_null() { self.message = json!({"role":"assistant","content":""}); }
        if delta.get("role").is_some_and(|v| v != "assistant") { return Err(response_error()); }
        if delta.get("refusal").is_some_and(truthy) { return Err(Error::new("model_refusal", "模型拒绝了本次请求；未执行新的工具调用。")); }
        let mut changed = false;
        for key in ["content", "reasoning_content"] {
            if let Some(part) = delta.get(key).filter(|v| !v.is_null()) {
                let part = part.as_str().ok_or_else(response_error)?;
                let mut text = self.message[key].as_str().unwrap_or("").to_owned(); text.push_str(part);
                self.message[key] = json!(text);
                changed |= key == "content" && !part.is_empty();
            }
        }
        if let Some(calls) = delta.get("tool_calls").filter(|v| !v.is_null()) {
            if self.message.get("tool_calls").is_none() { self.message["tool_calls"] = json!([]); }
            let assembled = self.message["tool_calls"].as_array_mut().unwrap();
            for call in calls.as_array().ok_or_else(response_error)? {
                let index = call["index"].as_u64().filter(|i| *i < 6).ok_or_else(response_error)? as usize;
                if index > assembled.len() { return Err(response_error()); }
                if index == assembled.len() { assembled.push(json!({"id":"","type":"function","function":{"name":"","arguments":""}})); }
                if call.get("type").is_some_and(|v| v != "function") { return Err(response_error()); }
                for (key, target) in [("id", "id"), ("name", "name"), ("arguments", "arguments")] {
                    let part = if key=="id" { call.get(key) } else { call["function"].get(key) };
                    if let Some(part) = part.filter(|v| !v.is_null()) {
                        let part = part.as_str().ok_or_else(response_error)?;
                        let dest = if key=="id" { &mut assembled[index][target] } else { &mut assembled[index]["function"][target] };
                        *dest = json!(format!("{}{}",dest.as_str().unwrap_or(""),part));
                    }
                }
            }
        }
        if let Some(reason) = choice.get("finish_reason").filter(|v| !v.is_null()) {
            self.finish_reason = Some(reason.as_str().ok_or_else(response_error)?.to_owned());
        }
        if changed { publish(self.message["content"].as_str().unwrap_or("")); }
        Ok(())
    }
    fn finish(self) -> Result<Value> {
        if !self.done || !matches!(self.finish_reason.as_deref(), Some("stop" | "tool_calls")) {
            return Err(Error::new("incomplete_model_response", "模型响应中断或被截断；未执行未完成的工具调用。"));
        }
        validated_message(self.message)
    }
}
impl Client {
    pub fn new() -> Result<Self> {
        let http = reqwest::Client::builder()
            .no_proxy()
            .redirect(reqwest::redirect::Policy::none())
            .retry(reqwest::retry::never())
            .connect_timeout(Duration::from_secs(20))
            .timeout(Duration::from_secs(120))
            .pool_idle_timeout(Duration::from_secs(90))
            .build()
            .map_err(|_| Error::new("model_connection", "模型网络初始化失败。"))?;
        Ok(Self { http })
    }
    async fn read(
        &self,
        mut response: reqwest::Response,
        max: usize,
        code: &'static str,
    ) -> Result<Value> {
        let invalid = || Error::new(code, "模型返回的数据过大或格式不兼容；本轮已停止。");
        if response.content_length().is_some_and(|n| n > max as u64) {
            return Err(invalid());
        }
        let mut body = Vec::new();
        while let Some(chunk) = response.chunk().await.map_err(network_error)? {
            if body.len() + chunk.len() > max {
                return Err(invalid());
            }
            body.extend_from_slice(&chunk);
        }
        strict_json::parse(&body).map_err(|_| invalid())
    }
    pub async fn complete(&self, input: &Value) -> Result<Value> {
        self.request(input, None).await
    }
    pub async fn stream(&self, input: &Value, publish: &mut dyn FnMut(&str)) -> Result<Value> {
        self.request(input, Some(publish)).await
    }
    async fn request(&self, input: &Value, mut publish: Option<&mut dyn FnMut(&str)>) -> Result<Value> {
        self.request_measured(input, publish.take(), &mut json!({})).await
    }
    pub async fn stream_measured(&self, input: &Value, publish: &mut dyn FnMut(&str), metrics: &mut Value) -> Result<Value> {
        self.request_measured(input, Some(publish), metrics).await
    }
    async fn request_measured(&self, input: &Value, mut publish: Option<&mut dyn FnMut(&str)>, metrics: &mut Value) -> Result<Value> {
        let started = Instant::now();
        *metrics = json!({});
        let settings = Settings::parse(&input["settings"])?;
        let mut body = payload(
            &settings.model,
            &input["messages"],
            &input["tools"],
            input["force_tool"].as_str(),
        )?;
        if publish.is_some() {
            let mut value: Value = serde_json::from_slice(&body).map_err(|_| response_error())?;
            value["stream"] = json!(true);
            body = serde_json::to_vec(&value).map_err(|_| response_error())?;
        }
        if body.len() > 900_000 {
            return Err(Error::new(
                "context_capacity",
                "本轮必要上下文过大；原记录已保留，请缩小任务范围。",
            ));
        }
        metrics["request_bytes"] = json!(body.len());
        metrics["message_count"] = json!(input["messages"].as_array().map_or(0, Vec::len));
        let response = self
            .http
            .post(format!("{}/chat/completions", settings.base_url))
            .bearer_auth(&settings.api_key)
            .header("Content-Type", "application/json")
            .body(body)
            .send()
            .await
            .map_err(network_error)?;
        metrics["headers_ms"] = json!(started.elapsed().as_millis() as u64);
        metrics["http_status"] = json!(response.status().as_u16());
        if response.status().as_u16() != 200 {
            return Err(http_failure(response.status().as_u16()));
        }
        if publish.is_some() && response.headers().get("content-type")
            .and_then(|v| v.to_str().ok()).is_some_and(|v| v.starts_with("text/event-stream")) {
            let mut response = response;
            let mut stream = CompletionStream::default();
            while let Some(chunk) = response.chunk().await.map_err(network_error)? {
                if metrics["first_chunk_ms"].is_null() { metrics["first_chunk_ms"] = json!(started.elapsed().as_millis() as u64); }
                stream.feed(&chunk, publish.as_deref_mut().unwrap())?;
                for (key, metric) in [("content", "first_content_ms"), ("reasoning_content", "first_reasoning_ms"), ("tool_calls", "first_tool_ms")] {
                    if metrics[metric].is_null() && truthy(&stream.message[key]) { metrics[metric] = json!(started.elapsed().as_millis() as u64); }
                }
                metrics["reasoning_chars"] = json!(stream.message["reasoning_content"].as_str().map_or(0, |s|s.chars().count()));
                if stream.done { break; }
            }
            metrics["total_ms"] = json!(started.elapsed().as_millis() as u64);
            return stream.finish();
        }
        let value = self
            .read(response, 2 * 1024 * 1024, "invalid_model_response")
            .await?;
        metrics["total_ms"] = json!(started.elapsed().as_millis() as u64);
        metrics["transport"] = json!("json");
        let choice = value["choices"]
            .as_array()
            .and_then(|v| v.first())
            .ok_or_else(response_error)?;
        if matches!(
            choice["finish_reason"].as_str(),
            Some("length" | "content_filter")
        ) {
            return Err(Error::new(
                "incomplete_model_response",
                "模型响应被截断或过滤；本轮未执行这些调用。",
            ));
        }
        validated_message(choice["message"].clone())
    }
    pub async fn list(&self, input: &Value) -> Result<Value> {
        let settings = Settings::parse(&input["settings"])?;
        let response = self
            .http
            .get(format!("{}/models", settings.base_url))
            .bearer_auth(&settings.api_key)
            .timeout(Duration::from_secs(20))
            .send()
            .await
            .map_err(network_error)?;
        let status = response.status().as_u16();
        if matches!(status, 404 | 405) {
            return Ok(json!({"models": [], "available": false,
            "note": "此服务没有模型列表，请填写服务商提供的模型名，再保存并测试。"}));
        }
        if status != 200 {
            return Err(http_failure(status));
        }
        let value = self
            .read(response, 1024 * 1024, "model_list_invalid")
            .await?;
        let items = value["data"].as_array().ok_or_else(|| {
            Error::new(
                "model_list_invalid",
                "服务未返回兼容的模型列表，可手动填写模型名后测试。",
            )
        })?;
        let mut seen = HashSet::new();
        let ids: Vec<_> = items
            .iter()
            .filter_map(|v| v["id"].as_str())
            .filter(|v| {
                (1..=160).contains(&v.chars().count())
                    && !v.chars().any(|c| c.is_whitespace() || c.is_control())
                    && seen.insert(*v)
            })
            .take(200)
            .collect();
        Ok(json!({"available": !ids.is_empty(), "models": ids,
            "note": "选择模型后保存并测试；列表不代表模型支持工具调用。"}))
    }
}

#[cfg(test)]
mod streaming_tests {
    use super::*;
    fn event(delta: Value, reason: Value) -> String {
        format!("data: {}\r\n\r\n", json!({"choices":[{"index":0,"delta":delta,"finish_reason":reason}]}))
    }
    #[test]
    fn utf8_fragmentation_publishes_only_real_content() {
        let raw = event(json!({"role":"assistant","reasoning_content":"private reasoning"}), Value::Null)
            + &event(json!({"content":"你好"}),Value::Null)
            + &event(json!({"content":"，同学。"}),Value::Null)
            + &event(json!({}),json!("stop"))+"data: [DONE]\r\n\r\n";
        let mut s=CompletionStream::default();let mut parts=Vec::new();
        for b in raw.bytes(){s.feed(&[b],&mut |t|parts.push(t.to_owned())).unwrap();}
        assert_eq!(parts,vec!["你好","你好，同学。"]);
        assert_eq!(s.finish().unwrap()["content"],"你好，同学。");
    }
    #[test]
    fn tool_fragments_are_assembled_then_validated() {
        let raw=event(json!({"tool_calls":[{"index":0,"id":"call_1","type":"function","function":{"name":"look","arguments":"{\"x\":"}}]}),Value::Null)
          + &event(json!({"tool_calls":[{"index":0,"function":{"name":"up","arguments":"1}"}}]}),json!("tool_calls"))+"data: [DONE]\n\n";
        let mut s=CompletionStream::default();let mut parts=Vec::new();
        for chunk in raw.as_bytes().chunks(7){s.feed(chunk,&mut |t|parts.push(t.to_owned())).unwrap();}
        let m=s.finish().unwrap();assert!(parts.is_empty());
        assert_eq!(m["tool_calls"][0]["function"],json!({"name":"lookup","arguments":"{\"x\":1}"}));
    }
    #[test]
    fn interruption_and_truncation_never_release_tools() {
        for reason in [Value::Null,json!("length"),json!("content_filter")] {
            let raw=event(json!({"content":"partial"}),reason);
            let mut s=CompletionStream::default();s.feed(raw.as_bytes(),&mut |_|{}).unwrap();
            assert_eq!(s.finish().unwrap_err().code,"incomplete_model_response");
        }
        let mut s=CompletionStream::default();s.feed((event(json!({"content":"partial"}),json!("stop"))+"data: [DONE]\n\n").as_bytes(),&mut |_|{}).unwrap();assert!(s.finish().is_ok());
    }
    #[test]
    fn refusal_bad_indices_and_size_fail_closed() {
        for delta in [json!({"refusal":"no"}),json!({"tool_calls":[{"index":6}]}),json!({"content":{}})] {
            let mut s=CompletionStream::default();assert!(s.feed(event(delta,Value::Null).as_bytes(),&mut |_|{}).is_err());
        }
        let mut s=CompletionStream::default();assert!(s.feed(&vec![b'a';2*1024*1024+1],&mut |_|{}).is_err());
    }
}
