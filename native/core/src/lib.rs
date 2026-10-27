//! Native application core; remaining business adapters use fixed private pipes.
pub mod agent;
pub mod app_io;
pub mod auth_http;
pub mod bb;
pub mod bb_io;
pub mod browser;
pub mod catalog;
pub mod cdp;
pub mod chat_io;
pub mod context;
pub mod desktop;
pub mod digest;
pub mod finance;
pub mod jw;
pub mod legacy;
pub mod login;
pub mod login_jobs;
pub mod mail;
pub mod mail_jobs;
pub mod model;
pub mod network;
pub mod public_sites;
pub mod recovery;
pub mod runner;
pub mod safety_log;
pub mod sessions;
pub mod schedule;
pub mod workbench;
pub mod storage;
pub mod strict_json;
pub mod young;
pub mod young_io;

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
    authenticated: auth_http::Client,
    browser: browser::Manager,
    login: login_jobs::Manager,
    mail: mail_jobs::Manager,
}
impl Core {
    pub fn new() -> Result<Self> {
        Ok(Self {
            model: model::Client::new()?,
            public: network::Client::new()?,
            authenticated: auth_http::Client::new()?,
            browser: browser::Manager::new(),
            login: login_jobs::Manager::default(),
            mail: mail_jobs::Manager::default(),
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
                    "network_policy", "public_read", "public_parse", "authenticated_read", "jw", "bb_adapter",
                    "young_parse", "young_policy", "young_job", "sessions_filter", "sessions_merge",
                    "sessions_read", "sessions_write", "sessions_device_save", "sessions_identity_save", "sessions_validate",
                    "browser_start", "browser_stop", "browser_status", "app_io", "bb_io", "young_io", "native_page_driver",
                    "login_begin", "login_status", "login_cancel", "chat_io", "desktop_backend",
                    "digest", "mail_parse", "mail_bind_begin", "mail_bind_status", "mail_bind_cancel",
                    "safety_log_record", "safety_log_report", "safety_log_summary", "safety_log_clear", "safety_log_export", "safety_log_clean"]})),
            "digest" => digest::dispatch(input),
            "mail_parse" => mail::parse(input),
            "mail_bind_begin" | "mail_bind_status" | "mail_bind_cancel" => {
                self.mail.dispatch(operation, input)
            }
            "safety_log_record" | "safety_log_report" | "safety_log_summary"
            | "safety_log_clear" | "safety_log_export" | "safety_log_clean" => {
                safety_log::dispatch(operation, input)
            }
            "login_begin" | "login_status" | "login_cancel" => {
                self.login.dispatch(operation, input)
            }
            "app_io" => app_io::dispatch(input, &self.model).await,
            "bb_io" => bb_io::dispatch(input).await,
            "young_io" => young_io::dispatch(input).await,
            "model_validate" => Ok(serde_json::to_value(model::Settings::parse(input)?)
                .map_err(|_| Error::invalid())?),
            "message_validate" => model::validated_message(input.clone()),
            "tool_arguments" => model::tool_arguments(input),
            "model_complete" => self.model.complete(input).await,
            "model_list" => self.model.list(input).await,
            "public_parse" => public_sites::parse_input(input),
            "authenticated_read" => self.authenticated.request(input).await,
            "jw" => jw::dispatch(input),
            "bb_adapter" => bb::dispatch(input),
            "young_parse" | "young_policy" | "young_job" => young::dispatch(operation, input),
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
