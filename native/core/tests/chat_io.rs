#![cfg(windows)]
//! Real local HTTP + Windows encrypted storage; no paid model or school requests.
use school_core::{
    app_io,
    chat_io::{self, Legacy},
    model, storage, Error, Result,
};
use serde_json::{json, Value};
use std::{
    fs,
    io::{Read, Write},
    net::TcpListener,
    path::{Path, PathBuf},
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc, Mutex,
    },
    thread,
    time::{Duration, Instant},
};

const CHAT: &str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
const TASK: &str = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";
fn rt() -> tokio::runtime::Runtime {
    tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .unwrap()
}
fn call(name: &str, args: Value, id: &str) -> Value {
    json!({"id":id,"type":"function","function":{"name":name,"arguments":args.to_string()}})
}
fn assistant(calls: Vec<Value>) -> Value {
    json!({"role":"assistant","tool_calls":calls})
}

struct Provider {
    url: String,
    requests: Arc<Mutex<Vec<Value>>>,
    done: Arc<AtomicBool>,
    thread: Option<thread::JoinHandle<()>>,
}
impl Provider {
    fn new(responses: Vec<(u16, Value, u64)>) -> Self {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let url = format!("http://{}", listener.local_addr().unwrap());
        listener.set_nonblocking(true).unwrap();
        let requests = Arc::new(Mutex::new(Vec::new()));
        let output = requests.clone();
        let done = Arc::new(AtomicBool::new(false));
        let stopped = done.clone();
        let thread = thread::spawn(move || {
            let mut responses = responses.into_iter();
            while !stopped.load(Ordering::Acquire) {
                let Ok((mut stream, _)) = listener.accept() else {
                    thread::sleep(Duration::from_millis(5));
                    continue;
                };
                // Windows inherits the listener's nonblocking mode. Read the
                // entire synthetic request before choosing its response.
                stream.set_nonblocking(false).unwrap();
                stream
                    .set_read_timeout(Some(Duration::from_secs(4)))
                    .unwrap();
                let mut raw = Vec::new();
                let mut buf = [0; 4096];
                let (head, length) = loop {
                    let count = stream.read(&mut buf).unwrap_or(0);
                    if count == 0 {
                        return;
                    }
                    raw.extend_from_slice(&buf[..count]);
                    if let Some(index) = raw.windows(4).position(|w| w == b"\r\n\r\n") {
                        let header = String::from_utf8_lossy(&raw[..index]);
                        let length = header
                            .lines()
                            .find_map(|line| {
                                line.split_once(':')
                                    .filter(|(k, _)| k.eq_ignore_ascii_case("content-length"))
                                    .map(|(_, v)| v.trim().parse::<usize>().unwrap())
                            })
                            .unwrap_or(0);
                        break (index + 4, length);
                    }
                    assert!(raw.len() < 32_000);
                };
                while raw.len() < head + length {
                    let count = stream.read(&mut buf).unwrap();
                    if count == 0 {
                        return;
                    }
                    raw.extend_from_slice(&buf[..count]);
                    assert!(raw.len() < 1_000_000);
                }
                output
                    .lock()
                    .unwrap()
                    .push(serde_json::from_slice(&raw[head..head + length]).unwrap());
                let (status, message, delay) =
                    responses.next().expect("unexpected extra model request");
                thread::sleep(Duration::from_millis(delay));
                let body =
                    json!({"choices":[{"finish_reason":"stop","message":message}]}).to_string();
                let reply=format!("HTTP/1.1 {status} Synthetic\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",body.len());
                let _ = stream.write_all(reply.as_bytes());
            }
        });
        Self {
            url,
            requests,
            done,
            thread: Some(thread),
        }
    }
}
impl Drop for Provider {
    fn drop(&mut self) {
        self.done.store(true, Ordering::Release);
        if let Some(handle) = self.thread.take() {
            // An unexpected model request already fails the case via its
            // response/count assertions; avoid double-panic during teardown.
            let _ = handle.join();
        }
    }
}

struct Fixture {
    root: PathBuf,
}
impl Fixture {
    fn new(url: &str, tasks: bool) -> Self {
        let root = std::env::temp_dir().join(format!("school-chat-v8-{}", uuid::Uuid::new_v4()));
        fs::create_dir(&root).unwrap();
        rt().block_on(app_io::dispatch(
            &json!({"action":"initialize","root":root}),
            &model::Client::new().unwrap(),
        ))
        .unwrap();
        storage::private_write(&json!({"path":root.join("model-settings.dpapi"),"value":{"base_url":url,"model":"synthetic-model","api_key":"synthetic-key","tested":true}})).unwrap();
        let chat = json!({"id":CHAT,"title":"synthetic","messages":[],"events":[],"tasks":if tasks {vec![TASK]} else {vec![]},"allowed_files":[],"pending":null,"state":"idle"});
        storage::chat_save(
            &json!({"database":root.join("profile/desktop-chats.sqlite3"),"chat":chat}),
        )
        .unwrap();
        Self { root }
    }
    fn chat(&self) -> Value {
        read_chat(&self.root)
    }
    fn save(&self, chat: Value) {
        storage::chat_save(
            &json!({"database":self.root.join("profile/desktop-chats.sqlite3"),"chat":chat}),
        )
        .unwrap();
    }
    fn input(&self) -> Value {
        json!({"root":self.root,"chat_id":CHAT,"text":"synthetic user goal","execute_authorized":false})
    }
}
impl Drop for Fixture {
    fn drop(&mut self) {
        assert!(self.root.starts_with(std::env::temp_dir()));
        let _ = fs::remove_dir_all(&self.root);
    }
}
fn read_chat(root: &Path) -> Value {
    storage::chat_read(&json!({"database":root.join("profile/desktop-chats.sqlite3"),"id":CHAT}))
        .unwrap()
}

struct StudyAdapter {
    calls: Mutex<Vec<String>>,
    root: PathBuf,
    ambiguous: bool,
    stop_after_list: bool,
}
impl Legacy for StudyAdapter {
    fn call(&self, action:&str, input:Value, stop:&AtomicBool, _notify:&dyn Fn(&str,&str)) -> Result<Value> {
        match action {
            "connect"=>Ok(json!({"connected":true})),
            "study_flow"=>{
                let phase=input["action"].as_str().unwrap();
                self.calls.lock().unwrap().push(phase.into());
                let entry=json!({"course_id":"_1_1","content_id":"_2_1","title":"Lab1","entry_path":"/webapps/assignment/uploadAssignment?course_id=_1_1&content_id=_2_1&mode=view"});
                match phase {
                    "start"=>{
                        assert_eq!(input["assignments"],study_args()["assignments"]);
                        if self.stop_after_list {stop.store(true,Ordering::Release);}
                        Ok(json!({"query_id":"synthetic-study","state":if self.ambiguous{"completed"}else{"partial"},
                            "timetable":{"count":1,"activities":[{"name":"current","weeks":[8]}]},
                            "assignments":if self.ambiguous{json!([{"state":"needs_input","matches":[entry.clone(),entry]}])}else{json!([{"state":"pending","entry":entry,"link":{"label":"课程 · Lab1"}}])},
                            "next_tool":if self.ambiguous{Value::Null}else{json!("school_study_continue")}}))
                    },
                    "continue"=>{
                        assert!(!stop.load(Ordering::Acquire));
                        assert!(!self.ambiguous);
                        assert_eq!(input["query_id"],"synthetic-study");
                        // The quick result and source link must be durable before the slow read.
                        let chat=read_chat(&self.root);
                        assert_eq!(chat["study_snapshot"]["assignments"][0]["state"],"pending");
                        assert_eq!(chat["source_links"].as_array().unwrap().len(),1);
                        let mut result=chat["study_snapshot"].clone();
                        result["state"]=json!("completed");
                        result["next_tool"]=Value::Null;
                        result["assignments"][0]["state"]=json!("completed");
                        result["assignments"][0]["submission_state"]=json!("historical_attempt_present");
                        Ok(result)
                    },
                    _=>panic!("Unexpected study phase {phase}"),
                }
            },
            _=>panic!("Unexpected adapter action {action}"),
        }
    }
}
fn study_args()->Value {
    let date=chrono::Utc::now().with_timezone(&chrono::FixedOffset::east_opt(8*3600).unwrap()).date_naive().to_string();
    json!({"date":date,"assignments":[{"course_keyword":"课程","term":"2026SP","assignment_keyword":"Lab1"}]})
}
#[test]
fn study_batch_uses_two_model_rounds_and_links_survive_followup() {
    let provider=Provider::new(vec![
        (200,assistant(vec![call("school_study_query",study_args(),"study")]),0),
        (200,json!({"role":"assistant","content":"查询结果"}),0),
        (200,json!({"role":"assistant","content":"精简结果（模型漏掉链接）"}),0),
    ]);
    let fixture=Fixture::new(&provider.url,false);
    let adapter=StudyAdapter{calls:Mutex::new(vec![]),root:fixture.root.clone(),ambiguous:false,stop_after_list:false};
    let stop=AtomicBool::new(false);
    let result=rt().block_on(chat_io::run(&fixture.input(),&stop,&|_,_|{},&adapter)).unwrap();
    assert_eq!(result["state"],"completed");
    let original=fixture.chat();
    assert_eq!(original["source_links"].as_array().unwrap().len(),1);
    assert_eq!(provider.requests.lock().unwrap().len(),2);
    let data:Value=serde_json::from_str(original["messages"][2]["content"].as_str().unwrap()).unwrap();
    assert_eq!(data["timetable"]["count"],1);
    let result=rt().block_on(chat_io::run(&fixture.input(),&stop,&|_,_|{},&adapter)).unwrap();
    assert_eq!(result["state"],"completed");
    assert_eq!(fixture.chat()["source_links"],original["source_links"]);
    assert_eq!(*adapter.calls.lock().unwrap(),vec!["start","continue"]);
    assert_eq!(fixture.chat()["timings"].as_array().unwrap().iter().filter(|t|t["stage"]=="model").count(),1);
}
#[test]
fn study_batch_does_not_inspect_ambiguous_assignment_or_continue_after_stop() {
    for stopped in [false,true] {
        let mut replies=vec![(200,assistant(vec![call("school_study_query",study_args(),"study")]),0)];
        if !stopped {replies.push((200,json!({"role":"assistant","content":"需要选择作业"}),0));}
        let provider=Provider::new(replies);
        let fixture=Fixture::new(&provider.url,false);
        let adapter=StudyAdapter{calls:Mutex::new(vec![]),root:fixture.root.clone(),ambiguous:!stopped,stop_after_list:stopped};
        let _=rt().block_on(chat_io::run(&fixture.input(),&AtomicBool::new(false),&|_,_|{},&adapter)).unwrap();
        assert_eq!(*adapter.calls.lock().unwrap(),vec!["start"]);
        assert_eq!(fixture.chat()["source_links"].as_array().unwrap().len(),usize::from(stopped));
        assert_eq!(fixture.chat()["study_snapshot"]["assignments"][0]["state"],if stopped{"pending"}else{"needs_input"});
    }
}

struct Adapter {
    calls: Mutex<Vec<(String, Value)>>,
    root: PathBuf,
    stop_after_dispatch: bool,
    fail_context: bool,
    invalid_artifact: bool,
    artifact_problem: bool,
}
impl Adapter {
    fn new(root: &Path) -> Self {
        Self {
            calls: Mutex::new(vec![]),
            root: root.to_path_buf(),
            stop_after_dispatch: false,
            fail_context: false,
            invalid_artifact: false,
            artifact_problem: false,
        }
    }
}
impl Legacy for Adapter {
    fn call(
        &self,
        action: &str,
        input: Value,
        stop: &AtomicBool,
        _notify: &dyn Fn(&str, &str),
    ) -> Result<Value> {
        self.calls
            .lock()
            .unwrap()
            .push((action.to_owned(), input.clone()));
        match action {
            "task_context" => {
                if self.fail_context {
                    return Err(Error::new(
                        "context_task_unavailable",
                        "synthetic identity mismatch",
                    ));
                }
                Ok(
                    json!({"task_id":TASK,"workflow":"mail","goal":"original exact user goal","state":"active","links":[],"calls":[{"tool":"school_mail_send","mode":"execute","arguments":{"draft_id":"original-reference"},"authorized":true,"status":"returned","receipt":{"state":"sent"}}]}),
                )
            }
            "connect" => Ok(json!({"connected":true})),
            "diagnostic" => Ok(input["problem"].clone()),
            "tool_dispatch" => {
                assert!(
                    read_chat(&self.root)["pending"].is_null(),
                    "confirmation must be consumed before IO"
                );
                if self.stop_after_dispatch {
                    stop.store(true, Ordering::Release);
                }
                let result = if input["name"] == "school_agent_start" {
                    json!({"task_id":TASK,"workflow":input["arguments"]["workflow"],"state":"active"})
                } else {
                    json!({"state":"verified","receipt_marker":"synthetic-issued-result"})
                };
                let mut envelope = json!({"result":result,"artifacts":if self.invalid_artifact {json!([{"path":"invalid"}])}else{json!([])}});
                if self.artifact_problem {
                    envelope["artifact_problem"] =
                        school_core::recovery::failure(&json!({"code":"file_not_authorized"}))?;
                }
                Ok(envelope)
            }
            _ => panic!("unexpected action"),
        }
    }
}

#[test]
fn followup_preserves_the_original_conversation_title() {
    let reply = json!({"role":"assistant","content":"synthetic final"});
    let provider = Provider::new(vec![(200, reply.clone(), 0), (200, reply, 0)]);
    let fixture = Fixture::new(&provider.url, false);
    let mut chat = fixture.chat();
    chat["messages"] = json!([{"role":"assistant","content":"欢迎使用"}]);
    fixture.save(chat);
    let adapter = Adapter::new(&fixture.root);
    let mut input = fixture.input();
    input["text"] = json!("查看本周课程和作业");
    rt().block_on(chat_io::run(
        &input, &AtomicBool::new(false), &|_, _| {}, &adapter,
    )).unwrap();
    let mut chat = fixture.chat();
    assert_eq!(chat["title"], "查看本周课程和作业");
    chat["title"] = json!("已有的课程安排记录");
    fixture.save(chat);
    input["text"] = json!("谢谢，再看一下下周的");
    rt().block_on(chat_io::run(
        &input, &AtomicBool::new(false), &|_, _| {}, &adapter,
    )).unwrap();
    let chat = fixture.chat();
    assert_eq!(chat["title"], "已有的课程安排记录");
    assert_eq!(
        chat["messages"].as_array().unwrap().iter()
            .filter(|m| m["role"] == "user").count(),
        2
    );
    assert_eq!(provider.requests.lock().unwrap().len(), 2);
}

#[test]
fn real_provider_uses_compiled_tools_executes_only_first_and_saves_final() {
    let provider = Provider::new(vec![
        (
            200,
            assistant(vec![
                call("school_agent_route", json!({"goal":"synthetic"}), "first"),
                call(
                    "school_agent_start",
                    json!({"workflow":"mail","goal":"must not run"}),
                    "second",
                ),
            ]),
            0,
        ),
        (
            200,
            json!({"role":"assistant","content":"synthetic final"}),
            0,
        ),
    ]);
    let fixture = Fixture::new(&provider.url, false);
    let adapter = Adapter::new(&fixture.root);
    let result = rt()
        .block_on(chat_io::run(
            &fixture.input(),
            &AtomicBool::new(false),
            &|_, _| {},
            &adapter,
        ))
        .unwrap();
    assert_eq!(result["state"], "completed");
    assert_eq!(result["business_success_inferred"], false);
    let calls = adapter.calls.lock().unwrap();
    assert_eq!(
        calls.iter().filter(|(a, _)| a == "tool_dispatch").count(),
        1
    );
    let chat = fixture.chat();
    assert_eq!(
        chat["messages"].as_array().unwrap().last().unwrap()["content"],
        "synthetic final"
    );
    let skipped: Value =
        serde_json::from_str(chat["messages"][2]["content"].as_str().unwrap()).unwrap();
    assert_eq!(skipped["dispatched"], false);
    assert_eq!(
        provider.requests.lock().unwrap()[0]["tools"]
            .as_array()
            .unwrap()
            .len(),
        14
    );
    let requests=provider.requests.lock().unwrap();
    let tools=&requests[0]["tools"];
    for name in ["school_study_query","school_study_continue","school_study_status","school_schedule","school_workspace"] {
        assert!(tools.as_array().unwrap().iter().any(|tool|tool["function"]["name"]==name));
    }
}

#[test]
fn pending_is_consumed_once_and_returned_receipt_survives_stop() {
    let provider = Provider::new(vec![(
        200,
        assistant(vec![call(
            "school_agent_call",
            json!({"task_id":TASK,"tool_name":"school_mail_send","arguments":{"draft_id":"synthetic","authorized":true},"authorized":true}),
            "send",
        )]),
        0,
    )]);
    let fixture = Fixture::new(&provider.url, true);
    let mut adapter = Adapter::new(&fixture.root);
    let stopped = AtomicBool::new(false);
    let result = rt()
        .block_on(chat_io::run(
            &fixture.input(),
            &stopped,
            &|_, _| {},
            &adapter,
        ))
        .unwrap();
    assert_eq!(result["state"], "waiting_for_approval");
    assert!(!adapter
        .calls
        .lock()
        .unwrap()
        .iter()
        .any(|(a, _)| a == "tool_dispatch"));
    let approval = fixture.chat()["pending"]["id"].clone();
    adapter.stop_after_dispatch = true;
    let input = json!({"root":fixture.root,"chat_id":CHAT,"approval":approval});
    stopped.store(true, Ordering::Release);
    let paused = rt()
        .block_on(chat_io::run(&input, &stopped, &|_, _| {}, &adapter))
        .unwrap();
    assert_eq!(paused["state"], "interrupted");
    assert_eq!(fixture.chat()["pending"]["id"], approval);
    assert!(!adapter
        .calls
        .lock()
        .unwrap()
        .iter()
        .any(|(a, _)| a == "tool_dispatch"));
    stopped.store(false, Ordering::Release);
    let result = rt()
        .block_on(chat_io::run(&input, &stopped, &|_, _| {}, &adapter))
        .unwrap();
    assert_eq!(result["state"], "interrupted");
    let chat = fixture.chat();
    assert!(chat["pending"].is_null());
    school_core::context::units(chat["messages"].as_array().unwrap()).unwrap();
    assert!(chat["messages"]
        .as_array()
        .unwrap()
        .iter()
        .any(|v| v["content"]
            .as_str()
            .is_some_and(|s| s.contains("synthetic-issued-result"))));
    let calls = adapter.calls.lock().unwrap();
    let dispatched = calls
        .iter()
        .filter(|(a, _)| a == "tool_dispatch")
        .collect::<Vec<_>>();
    assert_eq!(dispatched.len(), 1);
    assert_eq!(dispatched[0].1["authorized"], true);
    assert_eq!(
        dispatched[0].1["arguments"]["arguments"]["authorized"],
        true
    );
    drop(calls);
    stopped.store(false, Ordering::Release);
    assert!(rt()
        .block_on(chat_io::run(&input, &stopped, &|_, _| {}, &adapter))
        .is_err());
    assert_eq!(provider.requests.lock().unwrap().len(), 1);
}

#[test]
fn late_model_response_is_cancelled_without_tool_dispatch() {
    let provider = Provider::new(vec![(
        200,
        assistant(vec![call(
            "school_agent_start",
            json!({"workflow":"mail","goal":"must not execute"}),
            "late",
        )]),
        500,
    )]);
    let fixture = Fixture::new(&provider.url, false);
    let adapter = Adapter::new(&fixture.root);
    let stopped = Arc::new(AtomicBool::new(false));
    let flag = stopped.clone();
    let requests = provider.requests.clone();
    let stop = thread::spawn(move || {
        let deadline = Instant::now() + Duration::from_secs(5);
        while requests.lock().unwrap().is_empty() && Instant::now() < deadline {
            thread::sleep(Duration::from_millis(5));
        }
        flag.store(true, Ordering::Release);
    });
    let result = rt()
        .block_on(chat_io::run(
            &fixture.input(),
            &stopped,
            &|_, _| {},
            &adapter,
        ))
        .unwrap();
    stop.join().unwrap();
    assert_eq!(result["state"], "interrupted");
    assert!(!adapter
        .calls
        .lock()
        .unwrap()
        .iter()
        .any(|(a, _)| a == "tool_dispatch"));
    assert_eq!(fixture.chat()["messages"].as_array().unwrap().len(), 1);
}

#[test]
fn compacted_context_keeps_exact_receipts_and_demands_restoration_before_write() {
    let provider = Provider::new(vec![
        (
            200,
            assistant(vec![call(
                "school_agent_call",
                json!({"task_id":TASK,"tool_name":"school_mail_send","arguments":{"draft_id":"new"}}),
                "blocked",
            )]),
            0,
        ),
        (
            200,
            json!({"role":"assistant","content":"restore required"}),
            0,
        ),
    ]);
    let fixture = Fixture::new(&provider.url, true);
    let adapter = Adapter::new(&fixture.root);
    let mut chat = fixture.chat();
    chat["messages"] = json!([{"role":"user","content":"never change exact user correction"},{"role":"assistant","content":"x".repeat(240_000)}]);
    fixture.save(chat);
    let mut input = fixture.input();
    input["execute_authorized"] = json!(true);
    let result = rt()
        .block_on(chat_io::run(
            &input,
            &AtomicBool::new(false),
            &|_, _| {},
            &adapter,
        ))
        .unwrap();
    assert_eq!(result["state"], "completed");
    let raw = provider.requests.lock().unwrap()[0].to_string();
    assert!(raw.contains("original-reference"));
    assert!(raw.contains("never change exact user correction"));
    assert!(!adapter
        .calls
        .lock()
        .unwrap()
        .iter()
        .any(|(a, _)| a == "tool_dispatch"));
    assert!(fixture.chat()["messages"]
        .to_string()
        .contains("needs_context"));
}

#[test]
fn missing_original_identity_blocks_compaction_before_model_request() {
    let provider = Provider::new(vec![]);
    let fixture = Fixture::new(&provider.url, true);
    let mut adapter = Adapter::new(&fixture.root);
    adapter.fail_context = true;
    let mut chat = fixture.chat();
    chat["messages"] = json!([{"role":"assistant","content":"x".repeat(240_000)}]);
    fixture.save(chat);
    let result = rt()
        .block_on(chat_io::run(
            &fixture.input(),
            &AtomicBool::new(false),
            &|_, _| {},
            &adapter,
        ))
        .unwrap();
    assert_eq!(result["state"], "interrupted");
    assert_eq!(result["error_code"], "context_task_unavailable");
    assert!(provider.requests.lock().unwrap().is_empty());
}

#[test]
fn provider_errors_are_saved_with_fixed_code_and_no_tool_dispatch() {
    let provider = Provider::new(vec![(401, json!({}), 0)]);
    let fixture = Fixture::new(&provider.url, false);
    let adapter = Adapter::new(&fixture.root);
    let result = rt()
        .block_on(chat_io::run(
            &fixture.input(),
            &AtomicBool::new(false),
            &|_, _| {},
            &adapter,
        ))
        .unwrap();
    assert_eq!(result["error_code"], "model_authentication");
    assert_eq!(fixture.chat()["last_error_code"], "model_authentication");
    assert!(!fixture.chat().to_string().contains("synthetic-key"));
    assert!(!adapter
        .calls
        .lock()
        .unwrap()
        .iter()
        .any(|(a, _)| a == "tool_dispatch"));
}

#[test]
fn bad_artifact_keeps_receipt_and_warns_model_before_next_request() {
    for legacy_problem in [false, true] {
        let provider = Provider::new(vec![
            (
                200,
                assistant(vec![call(
                    "school_agent_route",
                    json!({"goal":"synthetic"}),
                    "one",
                )]),
                0,
            ),
            (
                200,
                json!({"role":"assistant","content":"original receipt retained; file unavailable"}),
                0,
            ),
        ]);
        let fixture = Fixture::new(&provider.url, false);
        let mut adapter = Adapter::new(&fixture.root);
        adapter.invalid_artifact = !legacy_problem;
        adapter.artifact_problem = legacy_problem;
        let result = rt()
            .block_on(chat_io::run(
                &fixture.input(),
                &AtomicBool::new(false),
                &|_, _| {},
                &adapter,
            ))
            .unwrap();
        assert_eq!(result["state"], "completed");
        let chat = fixture.chat();
        assert!(chat["messages"]
            .to_string()
            .contains("synthetic-issued-result"));
        assert_eq!(chat["allowed_files"], json!([]));
        assert!(chat["events"]
            .to_string()
            .contains("返回文件未通过本机校验"));
        let next = provider.requests.lock().unwrap()[1].to_string();
        assert!(next.contains("local_artifact_problem"));
        assert!(next.contains("synthetic-issued-result"));
    }
}

#[test]
fn runtime_inputs_cannot_replace_settings_or_tools_and_concurrent_claim_stops_before_http() {
    let provider = Provider::new(vec![]);
    let fixture = Fixture::new(&provider.url, false);
    let adapter = Adapter::new(&fixture.root);
    let mut input = fixture.input();
    input["settings"] = json!({"model":"arbitrary"});
    assert!(rt()
        .block_on(chat_io::run(
            &input,
            &AtomicBool::new(false),
            &|_, _| {},
            &adapter
        ))
        .is_err());
    let path = fixture
        .root
        .join("profile")
        .join(format!(".chat-{CHAT}.lock"));
    let lock = fs::OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .open(path)
        .unwrap();
    lock.try_lock().unwrap();
    let error = rt()
        .block_on(chat_io::run(
            &fixture.input(),
            &AtomicBool::new(false),
            &|_, _| {},
            &adapter,
        ))
        .unwrap_err();
    assert_eq!(error.code, "operation_busy");
    assert!(provider.requests.lock().unwrap().is_empty());
}
