//! Lossless user constraints and receipts; compaction never grants authorization.
use crate::{model, Error, Result};
use serde_json::{json, Value};
use std::collections::HashSet;

fn incomplete() -> Error {
    Error::new(
        "context_incomplete_batch",
        "工具记录尚未闭合；请先恢复原对话并核实回执。",
    )
}
pub fn units(messages: &[Value]) -> Result<Vec<Vec<Value>>> {
    let mut groups: Vec<Vec<Value>> = Vec::new();
    for message in messages {
        if message["role"] == "tool"
            && groups
                .last()
                .is_some_and(|g| g[0]["tool_calls"].as_array().is_some_and(|c| !c.is_empty()))
        {
            groups
                .last_mut()
                .ok_or_else(incomplete)?
                .push(message.clone());
        } else {
            groups.push(vec![message.clone()]);
        }
    }
    for group in &groups {
        if group[0]["role"] == "tool" {
            return Err(incomplete());
        }
        if let Some(calls) = group[0]["tool_calls"].as_array().filter(|c| !c.is_empty()) {
            let expected: HashSet<_> = calls.iter().map(|c| c["id"].as_str()).collect();
            let actual: HashSet<_> = group
                .iter()
                .skip(1)
                .map(|c| c["tool_call_id"].as_str())
                .collect();
            if expected != actual
                || expected.contains(&None)
                || expected.len() != calls.len()
                || actual.len() != group.len() - 1
            {
                return Err(incomplete());
            }
        }
    }
    Ok(groups)
}
pub fn project(input: &Value) -> Result<Value> {
    let messages = input["messages"].as_array().ok_or_else(Error::invalid)?;
    let instructions = input["instructions"].as_str().ok_or_else(Error::invalid)?;
    let model = input["model"].as_str().ok_or_else(Error::invalid)?;
    let budget = match input.get("budget") {
        None | Some(Value::Null) => 192_000,
        Some(v) => v
            .as_u64()
            .filter(|b| (1..=900_000).contains(b))
            .ok_or_else(Error::invalid)? as usize,
    };
    let system = json!({"role": "system", "content": instructions});
    let mut full = vec![system.clone()];
    full.extend_from_slice(messages);
    let size = |m: &[Value]| -> Result<usize> {
        Ok(model::payload(model, &json!(m), &input["tools"], None)?.len())
    };
    let original_size = size(&full)?;
    // Validate complete batches even below the budget, rather than forwarding a
    // malformed tool transcript to a provider.
    let grouped = units(messages)?;
    if original_size <= budget {
        return Ok(json!({"messages": full,
        "report": {"compacted": false, "request_bytes": original_size}}));
    }
    let Some(memory) = input.get("memory") else {
        return Ok(json!({"needs_memory": true}));
    };
    let memory = json!({"role": "assistant", "content": serde_json::to_string(memory).map_err(|_| Error::invalid())?});
    let mut selected: HashSet<_> = grouped
        .iter()
        .enumerate()
        .filter(|(_, g)| g[0]["role"] == "user")
        .map(|(i, _)| i)
        .collect();
    let assemble = |selected: &HashSet<usize>| {
        let mut result = vec![system.clone(), memory.clone()];
        for (i, group) in grouped.iter().enumerate() {
            if selected.contains(&i) {
                result.extend_from_slice(group);
            }
        }
        result
    };
    if size(&assemble(&selected))? > budget {
        return Err(Error::new("context_capacity",
        "用户要求和任务回执已达到本轮容量；原记录仍保留。请新建对话处理新的独立需求，原任务先在任务记录中核实，不重复提交。"));
    }
    for index in (0..grouped.len()).rev() {
        if selected.contains(&index) {
            continue;
        }
        selected.insert(index);
        if size(&assemble(&selected))? > budget {
            selected.remove(&index);
            break;
        }
    }
    let projected = assemble(&selected);
    Ok(
        json!({"report": {"compacted": true, "original_bytes": original_size,
        "request_bytes": size(&projected)?, "omitted_units": grouped.len() - selected.len()}, "messages": projected}),
    )
}
