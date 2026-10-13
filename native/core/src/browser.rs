//! Own the browser process tree for native CDP and remaining Playwright adapters.
//! This private-pipe API cannot select an executable, profile, flags, or URL.
use crate::{Error, Result};
use serde_json::{json, Value};
use std::{
    collections::HashMap,
    fs,
    io::Read,
    path::{Path, PathBuf},
    sync::Mutex,
    thread,
    time::{Duration, Instant},
};
use uuid::Uuid;

const MAX_BROWSERS: usize = 4;

fn failure() -> Error {
    Error::new(
        "browser_unavailable",
        "后台 Chrome 未能启动或已经停止。请检查本机 Chrome 后重试；没有执行学校操作。",
    )
}

fn executable() -> Result<PathBuf> {
    let mut paths = Vec::new();
    #[cfg(windows)]
    for key in ["PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"] {
        if let Some(base) = std::env::var_os(key) {
            paths.push(PathBuf::from(base).join("Google/Chrome/Application/chrome.exe"));
        }
    }
    #[cfg(target_os = "macos")]
    paths.push(PathBuf::from(
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    ));
    #[cfg(all(unix, not(target_os = "macos")))]
    for path in [
        "/opt/google/chrome/chrome",
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
    ] {
        paths.push(PathBuf::from(path));
    }
    paths
        .into_iter()
        .find(|p| p.is_absolute() && p.is_file())
        .ok_or_else(failure)
}

fn arguments(profile: &Path, headless: bool) -> Vec<String> {
    let mut args = vec![
        format!("--user-data-dir={}", profile.display()),
        "--remote-debugging-address=127.0.0.1".into(),
        "--remote-debugging-port=0".into(),
        "--no-first-run".into(),
        "--no-default-browser-check".into(),
        "--disable-background-networking".into(),
        "--disable-component-update".into(),
        "--disable-default-apps".into(),
        "--disable-sync".into(),
        "--disable-features=Translate,MediaRouter,OptimizationHints".into(),
        "--disable-popup-blocking".into(),
        "--password-store=basic".into(),
    ];
    if headless {
        args.push("--headless=new".into());
    }
    args.push("about:blank".into());
    args
}

fn endpoint(profile: &Path) -> Option<String> {
    let mut data = Vec::new();
    fs::File::open(profile.join("DevToolsActivePort"))
        .ok()?
        .take(513)
        .read_to_end(&mut data)
        .ok()?;
    if data.len() > 512 {
        return None;
    }
    let text = std::str::from_utf8(&data).ok()?;
    let mut lines = text.lines();
    let port = lines.next()?.parse::<u16>().ok()?;
    let path = lines.next()?;
    if port == 0
        || !path.starts_with("/devtools/browser/")
        || path.len() > 128
        || !path
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || matches!(c, '/' | '-'))
    {
        return None;
    }
    Some(format!("http://127.0.0.1:{port}"))
}

struct OwnedChrome {
    process: platform::Process,
    profile: PathBuf,
    headless: bool,
    endpoint: String,
}
impl OwnedChrome {
    fn start(headless: bool) -> Result<Self> {
        let executable = executable()?;
        // create_dir never follows an existing profile, including an attacker-created symlink.
        let profile = std::env::temp_dir()
            .canonicalize()
            .map_err(|_| failure())?
            .join(format!("school-core-chrome-{}", Uuid::new_v4()));
        Self::start_in(&executable, profile, headless)
    }
    fn start_in(executable: &Path, profile: PathBuf, headless: bool) -> Result<Self> {
        fs::create_dir(&profile).map_err(|_| failure())?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            if fs::set_permissions(&profile, fs::Permissions::from_mode(0o700)).is_err() {
                let _ = fs::remove_dir(&profile);
                return Err(failure());
            }
        }
        let process =
            match platform::Process::spawn(executable, &arguments(&profile, headless), headless) {
                Ok(p) => p,
                Err(e) => {
                    let _ = fs::remove_dir(&profile);
                    return Err(e);
                }
            };
        let mut owned = Self {
            process,
            profile,
            headless,
            endpoint: String::new(),
        };
        let deadline = Instant::now() + Duration::from_secs(15);
        while Instant::now() < deadline {
            if !owned.process.running() {
                return Err(failure());
            }
            if let Some(value) = endpoint(&owned.profile) {
                owned.endpoint = value;
                return Ok(owned);
            }
            thread::sleep(Duration::from_millis(50));
        }
        Err(failure())
    }
}
impl Drop for OwnedChrome {
    fn drop(&mut self) {
        self.process.stop();
        // Only this generated, initially nonexistent path is removed, after its process tree exits.
        // Never enumerate/delete other profiles or follow a replaced top-level symlink.
        for _ in 0..20 {
            let safe = fs::symlink_metadata(&self.profile)
                .is_ok_and(|m| m.is_dir() && !m.file_type().is_symlink());
            if !safe || fs::remove_dir_all(&self.profile).is_ok() {
                break;
            }
            thread::sleep(Duration::from_millis(50));
        }
    }
}

#[derive(Default)]
pub struct Manager {
    browsers: Mutex<HashMap<String, OwnedChrome>>,
}
impl Manager {
    pub fn new() -> Self {
        Self::default()
    }
    pub fn dispatch(&self, operation: &str, input: &Value) -> Result<Value> {
        let values = input.as_object().ok_or_else(Error::invalid)?;
        let mut browsers = self.browsers.lock().map_err(|_| failure())?;
        match operation {
            "browser_start" => {
                if values.keys().any(|k| k != "headless") {
                    return Err(Error::invalid());
                }
                let headless = match values.get("headless") {
                    None => true,
                    Some(v) => v.as_bool().ok_or_else(Error::invalid)?,
                };
                if browsers.len() >= MAX_BROWSERS {
                    return Err(Error::new(
                        "browser_busy",
                        "后台浏览器已达并发上限，请先结束已有任务。",
                    ));
                }
                let browser = OwnedChrome::start(headless)?;
                let id = Uuid::new_v4().to_string();
                let result =
                    json!({"browser_id": id, "endpoint": browser.endpoint, "headless": headless});
                browsers.insert(id, browser);
                Ok(result)
            }
            "browser_stop" | "browser_status" => {
                if values.len() != 1 {
                    return Err(Error::invalid());
                }
                let id = values
                    .get("browser_id")
                    .and_then(Value::as_str)
                    .ok_or_else(Error::invalid)?;
                if Uuid::parse_str(id).is_err() {
                    return Err(Error::invalid());
                }
                if operation == "browser_stop" {
                    drop(browsers.remove(id));
                    return Ok(json!({"browser_id": id, "stopped": true}));
                }
                let state = browsers.get_mut(id);
                Ok(match state {
                    Some(b) => {
                        json!({"browser_id":id,"running":b.process.running(),"headless":b.headless})
                    }
                    None => json!({"browser_id":id,"running":false}),
                })
            }
            _ => Err(Error::invalid()),
        }
    }
}

#[cfg(windows)]
mod platform {
    use super::*;
    use std::{os::windows::ffi::OsStrExt, ptr};
    use windows_sys::Win32::{
        Foundation::{CloseHandle, HANDLE, WAIT_TIMEOUT},
        System::{JobObjects::*, Threading::*},
    };

    pub struct Process {
        process: HANDLE,
        job: HANDLE,
    }
    fn wide(value: &std::ffi::OsStr) -> Vec<u16> {
        value.encode_wide().chain(Some(0)).collect()
    }
    fn quote(value: &str) -> String {
        let mut out = String::from("\"");
        let mut slashes = 0;
        for c in value.chars() {
            if c == '\\' {
                slashes += 1;
                continue;
            }
            out.extend(std::iter::repeat_n(
                '\\',
                if c == '"' { 2 * slashes + 1 } else { slashes },
            ));
            out.push(c);
            slashes = 0;
        }
        out.extend(std::iter::repeat_n('\\', slashes * 2));
        out.push('"');
        out
    }
    impl Process {
        pub fn spawn(executable: &Path, args: &[String], headless: bool) -> Result<Self> {
            let app = wide(executable.as_os_str());
            let command = std::iter::once(executable.to_string_lossy().to_string())
                .chain(args.iter().cloned())
                .map(|s| quote(&s))
                .collect::<Vec<_>>()
                .join(" ");
            let mut command = wide(std::ffi::OsStr::new(&command));
            unsafe {
                let job = CreateJobObjectW(ptr::null(), ptr::null());
                if job.is_null() {
                    return Err(failure());
                }
                let mut limit: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = std::mem::zeroed();
                limit.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
                if SetInformationJobObject(
                    job,
                    JobObjectExtendedLimitInformation,
                    &limit as *const _ as *const _,
                    std::mem::size_of_val(&limit) as u32,
                ) == 0
                {
                    CloseHandle(job);
                    return Err(failure());
                }
                let mut startup: STARTUPINFOW = std::mem::zeroed();
                startup.cb = std::mem::size_of_val(&startup) as u32;
                if headless {
                    startup.dwFlags = STARTF_USESHOWWINDOW;
                    startup.wShowWindow = 0;
                }
                let mut info: PROCESS_INFORMATION = std::mem::zeroed();
                if CreateProcessW(
                    app.as_ptr(),
                    command.as_mut_ptr(),
                    ptr::null(),
                    ptr::null(),
                    0,
                    CREATE_SUSPENDED | CREATE_NO_WINDOW,
                    ptr::null(),
                    ptr::null(),
                    &startup,
                    &mut info,
                ) == 0
                {
                    CloseHandle(job);
                    return Err(failure());
                }
                // No Chrome instruction executes before the Job owns it and every future child.
                if AssignProcessToJobObject(job, info.hProcess) == 0
                    || ResumeThread(info.hThread) == u32::MAX
                {
                    TerminateProcess(info.hProcess, 1);
                    WaitForSingleObject(info.hProcess, 5000);
                    CloseHandle(info.hThread);
                    CloseHandle(info.hProcess);
                    CloseHandle(job);
                    return Err(failure());
                }
                CloseHandle(info.hThread);
                Ok(Self {
                    process: info.hProcess,
                    job,
                })
            }
        }
        pub fn running(&mut self) -> bool {
            unsafe {
                !self.process.is_null() && WaitForSingleObject(self.process, 0) == WAIT_TIMEOUT
            }
        }
        pub fn stop(&mut self) {
            unsafe {
                if !self.job.is_null() {
                    TerminateJobObject(self.job, 1);
                    CloseHandle(self.job);
                    self.job = ptr::null_mut();
                }
                if !self.process.is_null() {
                    WaitForSingleObject(self.process, 5000);
                    CloseHandle(self.process);
                    self.process = ptr::null_mut();
                }
            }
        }
    }
    impl Drop for Process {
        fn drop(&mut self) {
            self.stop();
        }
    }
    #[cfg(test)]
    mod tests {
        use super::*;
        #[test]
        fn quotes_paths_without_shell() {
            assert_eq!(quote("C:\\a b\\"), "\"C:\\a b\\\\\"");
            assert_eq!(quote("a\"b"), "\"a\\\"b\"");
        }
    }
}

#[cfg(unix)]
mod platform {
    use super::*;
    use std::{
        os::unix::process::CommandExt,
        process::{Child, Command, Stdio},
    };
    unsafe extern "C" {
        fn kill(pid: i32, signal: i32) -> i32;
    }
    pub struct Process {
        child: Child,
        stopped: bool,
    }
    impl Process {
        pub fn spawn(executable: &Path, args: &[String], _headless: bool) -> Result<Self> {
            let child = Command::new(executable)
                .args(args)
                .stdin(Stdio::null())
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .process_group(0)
                .spawn()
                .map_err(|_| failure())?;
            Ok(Self {
                child,
                stopped: false,
            })
        }
        pub fn running(&mut self) -> bool {
            // Do not reap before stop: retaining the child prevents PID/process-group reuse.
            !self.stopped && unsafe { kill(self.child.id() as i32, 0) == 0 }
        }
        pub fn stop(&mut self) {
            if !self.stopped {
                unsafe {
                    kill(-(self.child.id() as i32), 9);
                }
                let _ = self.child.wait();
                self.stopped = true;
            }
        }
    }
    impl Drop for Process {
        fn drop(&mut self) {
            self.stop();
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn strict_private_api() {
        let manager = Manager::new();
        for value in [
            json!({"executable":"anything"}),
            json!({"headless":"false"}),
            json!({"profile":"anything"}),
            json!({"url":"https://example.test"}),
        ] {
            assert!(manager.dispatch("browser_start", &value).is_err());
        }
        assert!(manager
            .dispatch("browser_stop", &json!({"browser_id":"1"}))
            .is_err());
        assert!(manager
            .dispatch(
                "browser_stop",
                &json!({"browser_id":Uuid::new_v4().to_string(),"pid":1})
            )
            .is_err());
        let missing = json!({"browser_id":Uuid::new_v4().to_string()});
        assert_eq!(
            manager.dispatch("browser_status", &missing).unwrap()["running"],
            false
        );
        assert_eq!(
            manager.dispatch("browser_stop", &missing).unwrap()["stopped"],
            true
        );
    }
    #[test]
    fn endpoint_file_is_bounded_and_loopback_only() {
        let dir = std::env::temp_dir().join(format!("school-browser-test-{}", Uuid::new_v4()));
        fs::create_dir(&dir).unwrap();
        for invalid in [
            "0\n/devtools/browser/abc",
            "1234\nhttps://attacker.invalid",
            "65536\n/devtools/browser/abc",
            "1234\n/devtools/browser/a?b",
        ] {
            fs::write(dir.join("DevToolsActivePort"), invalid).unwrap();
            assert!(endpoint(&dir).is_none());
        }
        fs::write(
            dir.join("DevToolsActivePort"),
            "1234\n/devtools/browser/abc-123\n",
        )
        .unwrap();
        assert_eq!(endpoint(&dir).unwrap(), "http://127.0.0.1:1234");
        fs::remove_dir_all(dir).unwrap();
    }
    #[test]
    fn failed_start_cleans_only_new_owned_directory() {
        let profile = std::env::temp_dir().join(format!("school-browser-test-{}", Uuid::new_v4()));
        let missing = profile.join("no-such-executable");
        assert!(OwnedChrome::start_in(&missing, profile.clone(), true).is_err());
        assert!(!profile.exists());
        fs::create_dir(&profile).unwrap();
        fs::write(profile.join("preserved"), b"synthetic").unwrap();
        assert!(OwnedChrome::start_in(&missing, profile.clone(), true).is_err());
        assert_eq!(fs::read(profile.join("preserved")).unwrap(), b"synthetic");
        fs::remove_dir_all(profile).unwrap();
    }
}
