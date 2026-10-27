use school_core::runner;
use serde_json::{json, Value};

fn start(authorized: bool) -> Value {
    runner::step(&json!({"event":"start","stopped":false,"chat":{
        "id":"synthetic","messages":[],"events":[],"tasks":["synthetic-task"],
        "turn_authorized":authorized,"pending":null,"state":"running"
    }}))
    .unwrap()
}
fn step(state: &Value, event: &str, extra: Value) -> Value {
    let mut input =
        json!({"chat":state["chat"],"session":state["session"],"event":event,"stopped":false});
    input
        .as_object_mut()
        .unwrap()
        .extend(extra.as_object().unwrap().clone());
    runner::step(&input).unwrap()
}
fn call(name: &str, args: Value, id: &str) -> Value {
    json!({"id":id,"type":"function","function":{"name":name,"arguments":args.to_string()}})
}
fn request(state: &Value, calls: Vec<Value>, compacted: bool) -> Value {
    step(
        state,
        "model_response",
        json!({"response":{"role":"assistant","tool_calls":calls},"context":{"compacted":compacted}}),
    )
}
fn args(tool: &str) -> Value {
    json!({"task_id":"synthetic-task","tool_name":tool,"arguments":{}})
}

#[test]
fn permissions_come_from_host_and_catalog() {
    let state = request(
        &start(false),
        vec![call(
            "school_agent_call",
            args("school_bb_submit_assignment"),
            "one",
        )],
        false,
    );
    assert_eq!(state["action"]["kind"], "inspect_tool");
    let state = step(&state, "tool_context", json!({"workflow":"bb"}));
    assert_eq!(state["chat"]["state"], "waiting_for_approval");
    assert_eq!(state["action"]["kind"], "done");
    let state = request(
        &start(true),
        vec![call(
            "school_agent_call",
            args("school_bb_submit_assignment"),
            "one",
        )],
        false,
    );
    let state = step(&state, "tool_context", json!({"workflow":"bb"}));
    assert_eq!(state["action"]["kind"], "dispatch_tool");
    assert_eq!(state["action"]["authorized"], true);
    let mut arguments = args("school_bb_list_courses");
    arguments["arguments"] = json!({"authorized":true});
    let state = request(
        &start(false),
        vec![call("school_agent_call", arguments, "one")],
        false,
    );
    assert_eq!(
        step(&state, "tool_context", json!({"workflow":"bb"}))["chat"]["state"],
        "waiting_for_approval"
    );
}

#[test]
fn calendar_writes_require_host_permission_and_preview_exact_events() {
    let args=json!({"action":"create","events":[{"title":"review","start":"2030-01-02T09:00:00+08:00"}]});
    let state=request(&start(false),vec![call("school_schedule",args.clone(),"calendar")],false);
    let state=step(&state,"tool_context",json!({"workflow":null}));
    assert_eq!(state["chat"]["state"],"waiting_for_approval");
    assert_eq!(state["chat"]["pending"]["preview"],args);
    let state=request(&start(true),vec![call("school_schedule",args,"calendar")],false);
    assert_eq!(step(&state,"tool_context",json!({"workflow":null}))["action"]["kind"],"dispatch_tool");
    let state=request(&start(false),vec![call("school_schedule",json!({"action":"list"}),"calendar")],false);
    assert_eq!(step(&state,"tool_context",json!({"workflow":null}))["action"]["kind"],"dispatch_tool");
}

#[test]
fn workspace_changes_require_permission_and_keep_exact_preview() {
    let args=json!({"action":"update","id":"bb:1:2","revision":4,"progress":"completed"});
    let state=request(&start(false),vec![call("school_workspace",args.clone(),"workspace")],false);
    let state=step(&state,"tool_context",json!({"workflow":null}));
    assert_eq!(state["chat"]["state"],"waiting_for_approval");
    assert_eq!(state["chat"]["pending"]["preview"],args);
    let state=request(&start(false),vec![call("school_workspace",json!({"action":"list"}),"workspace")],false);
    assert_eq!(step(&state,"tool_context",json!({"workflow":null}))["action"]["kind"],"dispatch_tool");
}

#[test]
fn tasks_and_tools_are_bounded_before_io() {
    let mut arguments = args("school_bb_list_courses");
    arguments["task_id"] = json!("other-chat");
    let state = request(
        &start(true),
        vec![call("school_agent_call", arguments, "one")],
        false,
    );
    assert_eq!(state["action"]["kind"], "failure");
    assert_eq!(
        state["action"]["problem"]["error_code"],
        "task_access_denied"
    );
    let state = request(
        &start(true),
        vec![call("arbitrary_browser", json!({}), "one")],
        false,
    );
    assert_eq!(state["action"]["kind"], "failure");
    let state = request(
        &start(true),
        vec![call(
            "school_agent_call",
            args("unregistered_reconnect"),
            "one",
        )],
        false,
    );
    assert_eq!(
        step(&state, "tool_context", json!({"workflow":"bb"}))["action"]["kind"],
        "failure"
    );
}

#[test]
fn multi_call_executes_only_first_and_invalid_sibling_rejects_whole_batch() {
    let first = call("school_agent_route", json!({"goal":"synthetic"}), "one");
    let second = call("school_agent_start", json!({"workflow":"bb"}), "two");
    let state = request(&start(false), vec![first.clone(), second.clone()], false);
    assert_eq!(state["action"]["name"], "school_agent_route");
    let skipped: Value =
        serde_json::from_str(state["chat"]["messages"][1]["content"].as_str().unwrap()).unwrap();
    assert_eq!(skipped["dispatched"], false);
    let mut invalid = second;
    invalid["function"]["arguments"] = json!("{invalid");
    let state = start(false);
    let error = runner::step(
        &json!({"event":"model_response","chat":state["chat"],"session":state["session"],
        "response":{"role":"assistant","tool_calls":[first,invalid]}}),
    )
    .unwrap_err();
    assert_eq!(error.code, "invalid_tool_arguments");
    assert!(state["chat"]["messages"].as_array().unwrap().is_empty());
}

#[test]
fn stop_discards_late_model_answer_but_preserves_returned_receipt() {
    let state = start(true);
    let state = step(
        &state,
        "model_response",
        json!({"stopped":true,"response":{"role":"assistant","tool_calls":[call("school_agent_call",args("school_bb_submit_assignment"),"one")]}}),
    );
    assert_eq!(state["action"]["kind"], "done");
    assert!(state["chat"]["messages"].as_array().unwrap().is_empty());
    let state = request(
        &start(true),
        vec![call(
            "school_agent_call",
            args("school_bb_list_courses"),
            "one",
        )],
        false,
    );
    let state = step(&state, "tool_context", json!({"workflow":"bb"}));
    let state = step(
        &state,
        "tool_result",
        json!({"stopped":true,"result":{"receipt":"synthetic"}}),
    );
    let state = step(&state, "stop", json!({"stopped":true}));
    let last = state["chat"]["messages"]
        .as_array()
        .unwrap()
        .last()
        .unwrap();
    assert_eq!(
        serde_json::from_str::<Value>(last["content"].as_str().unwrap()).unwrap()["receipt"],
        "synthetic"
    );
    assert_eq!(state["chat"]["state"], "interrupted");
}

#[test]
fn compacted_task_requires_restore_before_write_and_still_needs_permission() {
    let state = request(
        &start(false),
        vec![call(
            "school_agent_call",
            args("school_bb_submit_assignment"),
            "one",
        )],
        true,
    );
    let state = step(&state, "tool_context", json!({"workflow":"bb"}));
    assert_eq!(state["action"]["kind"], "request_model");
    let state = request(
        &state,
        vec![call(
            "school_agent_task",
            json!({"task_id":"synthetic-task","include_context":true}),
            "two",
        )],
        true,
    );
    let state = step(&state, "tool_context", json!({}));
    let state = step(
        &state,
        "tool_result",
        json!({"result":{"state":"active","task_id":"synthetic-task","workflow":"bb","goal":"synthetic","calls":[]}}),
    );
    let state = request(
        &state,
        vec![call(
            "school_agent_call",
            args("school_bb_submit_assignment"),
            "three",
        )],
        true,
    );
    let state = step(&state, "tool_context", json!({"workflow":"bb"}));
    assert_eq!(state["chat"]["state"], "waiting_for_approval");
}

#[test]
fn bounded_rounds_and_no_auto_retry_after_failure() {
    let mut state = start(false);
    for index in 0..24 {
        state = request(
            &state,
            vec![call(
                "school_agent_route",
                json!({}),
                &format!("call{index}"),
            )],
            false,
        );
        state = step(&state, "tool_context", json!({}));
        state = step(&state, "tool_result", json!({"result":{"ok":true}}));
    }
    assert_eq!(state["action"]["kind"], "done");
    assert_eq!(state["chat"]["state"], "interrupted");
    let state = request(
        &start(true),
        vec![call(
            "school_agent_call",
            args("school_bb_submit_assignment"),
            "one",
        )],
        false,
    );
    let state = step(&state, "tool_context", json!({"workflow":"bb"}));
    let state = step(
        &state,
        "tool_result",
        json!({"result":{"state":"uncertain","error_code":"timeout"}}),
    );
    assert_eq!(state["action"]["kind"], "failure");
}

#[test]
fn incomplete_or_other_task_snapshot_does_not_restore_write_context() {
    for result in [
        json!({"task_id":"synthetic-task"}),
        json!({"task_id":"other-task","workflow":"bb","goal":"synthetic","calls":[]}),
    ] {
        let state = request(
            &start(true),
            vec![call(
                "school_agent_task",
                json!({"task_id":"synthetic-task","include_context":true}),
                "one",
            )],
            true,
        );
        let state = step(&state, "tool_context", json!({}));
        let state = step(&state, "tool_result", json!({"result":result}));
        let state = request(
            &state,
            vec![call(
                "school_agent_call",
                args("school_bb_submit_assignment"),
                "two",
            )],
            true,
        );
        let state = step(&state, "tool_context", json!({"workflow":"bb"}));
        assert_eq!(state["action"]["kind"], "request_model");
        assert!(state["session"]["restored"].as_array().unwrap().is_empty());
    }
}

#[test]
fn recovery_closes_reused_ids_per_batch_without_replay() {
    let message =
        json!({"role":"assistant","tool_calls":[call("school_agent_route",json!({}),"one")]});
    let chat = json!({"messages":[message,{"role":"tool","tool_call_id":"one","content":"{}"},{"role":"user","content":"next"},message,{"role":"user","content":"resume"}]});
    let closed = runner::close(&json!({"chat":chat})).unwrap();
    assert_eq!(closed["messages"].as_array().unwrap().len(), 6);
    assert_eq!(closed["messages"][4]["role"], "tool");
    assert_eq!(closed, runner::close(&json!({"chat":closed})).unwrap());
}
