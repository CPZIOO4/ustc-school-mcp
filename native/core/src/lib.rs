//! Native business core. No UI, school credentials, or Python runtime dependency.
pub mod agent;
pub mod browser;
pub mod catalog;
pub mod context;
pub mod finance;
pub mod model;
pub mod network;
pub mod public_sites;
pub mod recovery;
pub mod runner;
pub mod sessions;
pub mod storage;
pub mod strict_json;

use serde::Serialize;
use serde_json::{json, Value};

#[derive(Debug, Serialize)]
pub struct Error {
    pub code: &'static str,
    pub message: String,
}
impl Error {
    pub fn new(code: &'static str, message: impl Into<String>) -> Self {
        Self {
            code,
            message: message.into(),
        }
    }
    pub fn invalid() -> Self {
        Self::new("invalid_arguments", "本机核心请求格式无效。")
    }
}
pub type Result<T> = std::result::Result<T, Error>;

pub struct Core {
    model: model::Client,
    public: network::Client,
    browser: browser::Manager,
}
impl Core {
    pub fn new() -> Result<Self> {
        Ok(Self {
            model: model::Client::new()?,
            public: network::Client::new()?,
            browser: browser::Manager::new(),
        })
    }
    pub async fn dispatch(&self, request: Value) -> Result<Value> {
        let operation = request
            .get("operation")
            .and_then(Value::as_str)
            .ok_or_else(Error::invalid)?;
        let input = request.get("input").ok_or_else(Error::invalid)?;
        match operation {
            "status" => Ok(json!({"protocol": 1, "version": env!("CARGO_PKG_VERSION"),
                "implementation": "rust", "capabilities": ["model_complete", "model_list",
                    "model_validate", "message_validate", "tool_arguments", "context_project",
                    "private_read", "private_write", "chat_save", "chat_read", "chat_list",
                    "finance_preflight", "finance_guide", "runner_step", "runner_close",
                    "agent_create", "agent_read", "agent_list", "agent_claim", "agent_finish", "agent_failure",
                    "catalog_route", "catalog_workflows", "catalog_workflow", "catalog_mode",
                    "catalog_entry", "catalog_describe", "recovery_failure", "recovery_returned_failure",
                    "network_policy", "public_read", "public_parse", "sessions_filter", "sessions_merge",
                    "sessions_read", "sessions_write", "sessions_device_save", "sessions_identity_save", "sessions_validate",
                    "browser_start", "browser_stop", "browser_status"]})),
            "model_validate" => Ok(serde_json::to_value(model::Settings::parse(input)?)
                .map_err(|_| Error::invalid())?),
            "message_validate" => model::validated_message(input.clone()),
            "tool_arguments" => model::tool_arguments(input),
            "model_complete" => self.model.complete(input).await,
            "model_list" => self.model.list(input).await,
            "public_parse" => public_sites::parse_input(input),
            "public_read" => {
                let planned = public_sites::request(input)?;
                let database = input["database"].as_str().ok_or_else(Error::invalid)?;
                let response = self.public.get_public(&json!({"database": database, "service": planned.service, "url": planned.url})).await?;
                if response["state"] != "response" {
                    return Ok(response);
                }
                let parsed = public_sites::parse(
                    input,
                    response["html"].as_str().ok_or_else(Error::invalid)?,
                    &planned.url,
                );
                network::policy(
                    &json!({"action": if parsed.is_ok() {"success"} else {"failure"}, "database":database, "service":planned.service, "now":network::now(), "jitter":0.5}),
                )?;
                Ok(json!({"state":"parsed", "result":parsed?}))
            }
            "network_policy" => network::policy(input),
            "browser_start" | "browser_stop" | "browser_status" => {
                self.browser.dispatch(operation, input)
            }
            "sessions_filter"
            | "sessions_merge"
            | "sessions_read"
            | "sessions_write"
            | "sessions_device_save"
            | "sessions_identity_save"
            | "sessions_validate" => sessions::dispatch(operation, input),
            "context_project" => context::project(input),
            "private_read" => storage::private_read(input),
            "private_write" => storage::private_write(input),
            "chat_save" => storage::chat_save(input),
            "chat_read" => storage::chat_read(input),
            "chat_list" => storage::chat_list(input),
            "runner_step" => runner::step(input),
            "runner_close" => runner::close(input),
            "agent_create" => agent::create(input),
            "agent_read" => agent::read(input),
            "agent_list" => agent::list(input),
            "agent_claim" => agent::claim(input),
            "agent_finish" => agent::finish(input),
            "agent_failure" => agent::failure(input),
            "catalog_route" | "catalog_workflows" | "catalog_workflow" | "catalog_mode"
            | "catalog_entry" | "catalog_describe" => catalog::dispatch(operation, input),
            "recovery_failure" => recovery::failure(input),
            "recovery_returned_failure" => recovery::returned(input),
            "finance_preflight" => finance::preflight(input),
            "finance_guide" => {
                finance::guide(input["business_type"].as_str().ok_or_else(Error::invalid)?)
            }
            _ => Err(Error::invalid()),
        }
    }
}
