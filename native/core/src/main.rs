//! Length-prefixed private pipes only. No listening TCP port or plaintext logs.
#![cfg_attr(windows, windows_subsystem = "windows")]
use school_core::{Core, Error};
use serde_json::json;
use std::io::{self, Read, Write};

fn main() {
    // Explicit invocation only; no secrets or user data in process arguments.
    let arguments = std::env::args().skip(1).collect::<Vec<_>>();
    let scheduled = arguments.len() == 5
        && arguments[0] == "--young-worker"
        && arguments[1] == "--schedule-id"
        && arguments[3] == "--private-dir";
    if arguments != ["--stdio"] && !scheduled {
        std::process::exit(2);
    }
    let runtime = match tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
    {
        Ok(v) => v,
        Err(_) => std::process::exit(1),
    };
    if scheduled {
        let result = runtime.block_on(school_core::young_io::run_schedule(
            std::path::Path::new(&arguments[4]),
            &arguments[2],
        ));
        std::process::exit(if result.is_ok() { 0 } else { 1 });
    }
    let core = match Core::new() {
        Ok(v) => v,
        Err(_) => std::process::exit(1),
    };
    let mut input = io::stdin().lock();
    let mut output = io::stdout().lock();
    loop {
        let mut header = [0u8; 4];
        if input.read_exact(&mut header).is_err() {
            break;
        }
        let size = u32::from_be_bytes(header) as usize;
        if size == 0 || size > 16 * 1024 * 1024 {
            break;
        }
        let mut bytes = vec![0; size];
        if input.read_exact(&mut bytes).is_err() {
            break;
        }
        let result = match school_core::strict_json::parse(&bytes) {
            Ok(request) => runtime.block_on(core.dispatch(request)),
            Err(_) => Err(Error::invalid()),
        };
        let envelope = match result {
            Ok(value) => json!({"ok": true, "result": value}),
            Err(error) => json!({"ok": false, "error": error}),
        };
        let bytes = match serde_json::to_vec(&envelope) {
            Ok(v) => v,
            Err(_) => break,
        };
        if bytes.len() > 16 * 1024 * 1024 {
            break;
        }
        if output
            .write_all(&(bytes.len() as u32).to_be_bytes())
            .is_err()
            || output.write_all(&bytes).is_err()
            || output.flush().is_err()
        {
            break;
        }
    }
}
