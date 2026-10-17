//! Pollable login tasks keep long manual verification off the private RPC loop.
use crate::{Error, Result};
use serde_json::{json, Value};
use std::{
    collections::HashMap,
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc, Mutex,
    },
    thread::JoinHandle,
};

struct Job {
    stopped: Arc<AtomicBool>,
    state: Arc<Mutex<Value>>,
    thread: Option<JoinHandle<()>>,
}
#[derive(Default)]
pub struct Manager {
    jobs: Mutex<HashMap<String, Job>>,
}
impl Manager {
    pub fn dispatch(&self, operation: &str, input: &Value) -> Result<Value> {
        let mut jobs = self.jobs.lock().map_err(|_| Error::invalid())?;
        if operation == "mail_bind_begin" {
            // One login at a time per private pipe prevents competing device-state writes.
            if jobs
                .values()
                .any(|j| j.thread.as_ref().is_some_and(|t| !t.is_finished()))
            {
                return Err(Error::new("mail_bind_busy", "请先完成或停止当前邮箱绑定。"));
            }
            if jobs.len() >= 16 {
                jobs.retain(|_, j| j.thread.as_ref().is_some_and(|t| !t.is_finished()));
            }
            let id = uuid::Uuid::new_v4().simple().to_string();
            let stopped = Arc::new(AtomicBool::new(false));
            let state = Arc::new(Mutex::new(
                json!({"state":"running","stage":"starting","detail":"正在打开官方邮箱绑定。"}),
            ));
            let (s, v, request) = (stopped.clone(), state.clone(), input.clone());
            let thread = std::thread::spawn(move || {
                let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                    tokio::runtime::Builder::new_current_thread()
                        .enable_all()
                        .build()
                        .map_err(|_| Error::new("runtime_failure", "邮箱绑定运行环境未能启动。"))
                        .and_then(|rt| {
                            rt.block_on(crate::mail::run(&request, &s, &|progress| {
                                if let Ok(mut value) = v.lock() {
                                    *value = json!({"state":"running","progress":progress});
                                }
                            }))
                        })
                }))
                .unwrap_or_else(|_| {
                    Err(Error::new(
                        "runtime_failure",
                        "邮箱绑定任务中断，已有会话保留。",
                    ))
                });
                if let Ok(mut value) = v.lock() {
                    *value = match result {
                        Ok(result) => {
                            json!({"state":"succeeded","result":result,"stop_requested":s.load(Ordering::SeqCst)})
                        }
                        Err(error) => {
                            json!({"state":if s.load(Ordering::SeqCst) {"cancelled"} else {"failed"},"error":error})
                        }
                    };
                }
            });
            jobs.insert(
                id.clone(),
                Job {
                    stopped,
                    state,
                    thread: Some(thread),
                },
            );
            return Ok(json!({"job_id":id,"state":"running"}));
        }
        let id = input["job_id"].as_str().ok_or_else(Error::invalid)?;
        let job = jobs.get(id).ok_or_else(Error::invalid)?;
        match operation {
            "mail_bind_status" => job
                .state
                .lock()
                .map(|v| v.clone())
                .map_err(|_| Error::invalid()),
            "mail_bind_cancel" => {
                job.stopped.store(true, Ordering::SeqCst);
                Ok(json!({"stop_requested":true}))
            }
            _ => Err(Error::invalid()),
        }
    }
}
impl Drop for Manager {
    fn drop(&mut self) {
        if let Ok(jobs) = self.jobs.get_mut() {
            for job in jobs.values() {
                job.stopped.store(true, Ordering::SeqCst);
            }
            for job in jobs.values_mut() {
                if let Some(thread) = job.thread.take() {
                    let _ = thread.join();
                }
            }
        }
    }
}
