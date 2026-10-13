//! Real headless Chrome, local synthetic HTTP only; no school access or Playwright.
use school_core::cdp::{Decision, Page};
use serde_json::json;
use std::{
    io::{Read, Write},
    net::TcpListener,
    sync::{
        atomic::{AtomicBool, AtomicUsize, Ordering},
        Arc, Mutex,
    },
    thread,
    time::Duration,
};
static SERIAL: Mutex<()> = Mutex::new(());
struct Site {
    base: String,
    posts: Arc<AtomicUsize>,
    gets: Arc<AtomicUsize>,
    stop: Arc<AtomicBool>,
    thread: Option<thread::JoinHandle<()>>,
}
impl Site {
    fn new() -> Self {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        listener.set_nonblocking(true).unwrap();
        let base = format!("http://{}", listener.local_addr().unwrap());
        let posts = Arc::new(AtomicUsize::new(0));
        let gets = Arc::new(AtomicUsize::new(0));
        let stop = Arc::new(AtomicBool::new(false));
        let (p, g, s) = (posts.clone(), gets.clone(), stop.clone());
        let handle = thread::spawn(move || {
            let mut connections = Vec::new();
            while !s.load(Ordering::SeqCst) {
                if let Ok((mut stream, _)) = listener.accept() {
                    let (p, g) = (p.clone(), g.clone());
                    connections.push(thread::spawn(move||{
                    stream
                        .set_read_timeout(Some(Duration::from_secs(2)))
                        .unwrap();
                    let mut buf = [0; 65536];
                    // Chrome opens idle preconnect sockets. Serve each accepted
                    // connection independently and consume the complete HTTP
                    // request before close, otherwise Windows may send TCP RST.
                    let mut n=0;
                    loop {
                        if n==buf.len(){return;}
                        let got=stream.read(&mut buf[n..]).unwrap_or(0);
                        if got==0{return;}
                        n+=got;
                        if let Some(end)=buf[..n].windows(4).position(|v|v==b"\r\n\r\n") {
                            let head=String::from_utf8_lossy(&buf[..end]);
                            let length=head.lines().filter_map(|line|line.split_once(':'))
                                .find(|(name,_)|name.eq_ignore_ascii_case("content-length"))
                                .map(|(_,v)|v.trim().parse::<usize>().unwrap()).unwrap_or(0);
                            if n>=end+4+length{break;}
                        }
                    }
                    let text = String::from_utf8_lossy(&buf[..n]);
                    if text.starts_with("POST /redirect ") {
                        p.fetch_add(1, Ordering::SeqCst);
                        let _=stream.write_all(b"HTTP/1.1 302 Found\r\nLocation: /redirect-destination\r\nContent-Length: 0\r\nConnection: close\r\n\r\n");
                        return;
                    }
                    let (body, kind) = if text.starts_with("POST /submit ") {
                        p.fetch_add(1, Ordering::SeqCst);
                        ("{\"accepted\":true}", "application/json")
                    } else if text.starts_with("GET /popup ") {
                        g.fetch_add(1, Ordering::SeqCst);
                        ("<!doctype html><script>fetch('/submit',{method:'POST',body:'popup'})</script>","text/html")
                    } else {
                        g.fetch_add(1, Ordering::SeqCst);
                        ("<!doctype html><html><head><title>Synthetic</title></head><body><input id='name'><button id='send' onclick=\"fetch('/submit',{method:'POST',headers:{'X-Synthetic':'fixture'},body:document.querySelector('#name').value}).then(r=>r.text()).then(t=>document.querySelector('#out').textContent=t).catch(()=>document.querySelector('#out').textContent='blocked')\">send</button><div id='out'></div></body></html>","text/html")
                    };
                    let response=format!("HTTP/1.1 200 OK\r\nContent-Type: {kind}; charset=utf-8\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",body.len());
                    let _ = stream.write_all(response.as_bytes());
                    }));
                } else {
                    thread::sleep(Duration::from_millis(5));
                }
            }
            for connection in connections {
                connection.join().unwrap();
            }
        });
        Self {
            base,
            posts,
            gets,
            stop,
            thread: Some(handle),
        }
    }
}
impl Drop for Site {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::SeqCst);
        self.thread.take().unwrap().join().unwrap();
    }
}

#[test]
fn worker_and_popup_cannot_send_unguarded_posts() {
    let _serial = SERIAL.lock().unwrap_or_else(|e| e.into_inner());
    let site = Site::new();
    tokio::runtime::Builder::new_current_thread().enable_all().build().unwrap().block_on(async{
        let prefix=site.base.clone();
        let mut page=Page::launch(&json!({"cookies":[],"origins":[]}),None,true,Box::new(move|event|{
            Ok(if event["request"]["method"]=="GET"&&event["request"]["url"].as_str().unwrap_or("").starts_with(&prefix){Decision::Continue}else{Decision::Abort})
        })).await.unwrap();
        page.navigate(&format!("{}/",site.base)).unwrap();page.clear_responses();
        let worker=format!("fetch({},{{method:'POST',body:'worker'}})",json!(format!("{}/submit",site.base)));
        page.evaluate(&format!("(()=>{{window.fixtureWorker=new Worker(URL.createObjectURL(new Blob([{}],{{type:'text/javascript'}})));return true;}})()",json!(worker))).expect("create worker");
        page.pump(300).expect("pump worker");
        let before=site.gets.load(Ordering::SeqCst);
        page.evaluate("window.open('/popup','fixture'); true").expect("create popup");
        page.pump(1200).unwrap();
        let targets=page.take_events("Target.attachedToTarget");
        assert!(targets.iter().any(|v|v["targetInfo"]["type"]=="worker"),"worker must actually start paused");
        assert!(targets.iter().any(|v|v["targetInfo"]["type"]=="page"),"popup must actually start paused");
        assert_eq!(site.posts.load(Ordering::SeqCst),0);
        assert_eq!(site.gets.load(Ordering::SeqCst),before,"even popup GET must be blocked");
        assert!(page.body_text().unwrap().contains("send"),"main page remains usable");
    });
}

#[test]
fn redirect_response_preserves_post_without_following_location() {
    let _serial = SERIAL.lock().unwrap_or_else(|e| e.into_inner());
    let site = Site::new();
    tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .unwrap()
        .block_on(async {
            let prefix = site.base.clone();
            let mut page = Page::launch(
                &json!({"cookies":[],"origins":[]}),
                None,
                true,
                Box::new(move |event| {
                    let req = &event["request"];
                    let url = req["url"].as_str().unwrap_or("");
                    Ok(
                        if url == format!("{prefix}/")
                            || (url == format!("{prefix}/redirect") && req["method"] == "POST")
                        {
                            Decision::Continue
                        } else {
                            Decision::Abort
                        },
                    )
                }),
            )
            .await
            .unwrap();
            page.navigate(&format!("{}/", site.base)).unwrap();
            page.clear_responses();
            page.evaluate("fetch('/redirect',{method:'POST',body:'once'}).catch(()=>{}); true")
                .unwrap();
            let response = page.wait_response("/redirect", 5000).unwrap();
            assert_eq!(response["status"], 302);
            assert_eq!(response["request"]["method"], "POST");
            assert_eq!(response["body"], "");
            assert_eq!(response["redirected"], true);
            page.pump(300).unwrap();
            assert_eq!(site.posts.load(Ordering::SeqCst), 1);
            assert_eq!(site.gets.load(Ordering::SeqCst), 1);
        });
}

#[test]
fn websocket_cannot_bypass_http_policy() {
    let _serial = SERIAL.lock().unwrap_or_else(|e| e.into_inner());
    let site = Site::new();
    tokio::runtime::Builder::new_current_thread().enable_all().build().unwrap().block_on(async{
        let prefix=site.base.clone();
        let mut page=Page::launch(&json!({"cookies":[],"origins":[]}),None,true,Box::new(move|event|{
            Ok(if event["request"]["url"]==format!("{prefix}/") {Decision::Continue}else{Decision::Abort})
        })).await.unwrap();
        page.navigate(&format!("{}/",site.base)).unwrap();page.clear_responses();
        let socket=format!("{}/socket",site.base.replace("http://","ws://"));
        assert_eq!(page.evaluate(&format!("(()=>{{try{{new WebSocket({});return false}}catch(e){{return e.name==='SecurityError'}}}})()",json!(socket))).unwrap(),true);
        page.pump(300).unwrap();
        assert_eq!(site.gets.load(Ordering::SeqCst),1,"no websocket handshake may reach the server");
    });
}

#[test]
fn late_request_policy_error_is_preserved_while_waiting_for_response() {
    let _serial = SERIAL.lock().unwrap_or_else(|e| e.into_inner());
    let site = Site::new();
    tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .unwrap()
        .block_on(async {
            let prefix = site.base.clone();
            let mut page = Page::launch(
                &json!({"cookies":[],"origins":[]}),
                None,
                true,
                Box::new(move |event| {
                    let url = event["request"]["url"].as_str().unwrap_or("");
                    if url.ends_with("/delayed") {
                        return Err(school_core::Error::new("rate_limited", "合成冷却信号"));
                    }
                    Ok(if url.starts_with(&prefix) {
                        Decision::Continue
                    } else {
                        Decision::Abort
                    })
                }),
            )
            .await
            .unwrap();
            page.navigate(&format!("{}/", site.base)).unwrap();
            page.clear_responses();
            page.evaluate("setTimeout(()=>fetch('/delayed').catch(()=>{}),150); true")
                .unwrap();
            let start = std::time::Instant::now();
            assert_eq!(
                page.wait_response("/delayed", 10000).unwrap_err().code,
                "rate_limited"
            );
            assert!(start.elapsed() < Duration::from_secs(3));
        });
}

#[test]
fn real_page_restore_fill_click_capture_and_once_only_request_guard() {
    let _serial = SERIAL.lock().unwrap_or_else(|e| e.into_inner());
    let site = Site::new();
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .unwrap();
    runtime.block_on(async{
        let prefix=format!("{}/",site.base);let used=Arc::new(AtomicBool::new(false));let used_guard=used.clone();
        let state=json!({"cookies":[],"origins":[{"origin":site.base,"localStorage":[{"name":"synthetic","value":"kept '\" 中文"}],"indexedDB":[{"name":"fixture","version":1,"stores":[{"name":"records","autoIncrement":false,"keyPath":"id","indexes":[],"records":[{"value":{"id":1,"name":"synthetic"}}]}]}]}]});
        let mut page=Page::launch(&state,None,true,Box::new(move|p|{
            let request=&p["request"];let url=request["url"].as_str().unwrap_or("");
            if !url.starts_with(&prefix){return Ok(Decision::Abort)}
            if request["method"]=="POST" {return Ok(if url.ends_with("/submit")&&!used_guard.swap(true,Ordering::SeqCst){Decision::Continue}else{Decision::Abort})}
            Ok(Decision::Continue)
        })).await.unwrap();
        assert_eq!(site.gets.load(Ordering::SeqCst),0,"storage restore must not contact the site");
        page.navigate(&format!("{}/",site.base)).unwrap();
        assert!(page.html().unwrap().contains("Synthetic"));assert!(page.body_text().unwrap().contains("send"));
        assert_eq!(page.evaluate("localStorage.getItem('synthetic')").unwrap(),"kept '\" 中文");
        assert_eq!(page.evaluate("(async()=>{const db=await new Promise(r=>{const q=indexedDB.open('fixture');q.onsuccess=()=>r(q.result)});return await new Promise(r=>{const q=db.transaction('records').objectStore('records').get(1);q.onsuccess=()=>{db.close();r(q.result.name)}})})()").unwrap(),"synthetic");
        page.fill("#name","synthetic payload").unwrap();page.clear_responses();page.click("#send").unwrap();
        let response=page.wait_response("/submit",10000).unwrap();
        assert_eq!(response["status"],200);assert_eq!(response["request"]["method"],"POST");
        assert_eq!(response["body"],"{\"accepted\":true}");assert_eq!(site.posts.load(Ordering::SeqCst),1);
        page.click("#send").unwrap();page.wait_expression("document.querySelector('#out').textContent === 'blocked'",5000).unwrap();
        assert_eq!(site.posts.load(Ordering::SeqCst),1);
        assert_eq!(page.evaluate("fetch('https://invalid.example.invalid/').then(()=>false).catch(()=>true)").unwrap(),true);
        assert_eq!(site.posts.load(Ordering::SeqCst),1);
    });
}

#[test]
fn frozen_document_and_session_isolation() {
    let _serial = SERIAL.lock().unwrap_or_else(|e| e.into_inner());
    let site = Site::new();
    tokio::runtime::Builder::new_current_thread().enable_all().build().unwrap().block_on(async{
        let origin=site.base.clone();let mut first=Page::launch(&json!({"cookies":[{"name":"session","value":"synthetic-a","domain":"127.0.0.1","path":"/","secure":false,"httpOnly":false}],"origins":[]}),None,true,Box::new(move|p|{
            Ok(if p["request"]["url"]==format!("{origin}/frozen") { Decision::Fulfill{status:200,headers:vec![("Content-Type".into(),"text/html".into())],body:b"<!doctype html><title>Frozen</title><body>frozen</body>".to_vec()} }else{Decision::Abort})
        })).await.unwrap();
        first.navigate(&format!("{}/frozen",site.base)).unwrap();assert_eq!(first.body_text().unwrap(),"frozen");
        assert!(first.evaluate("document.cookie").unwrap().as_str().unwrap().contains("synthetic-a"));
        assert_eq!(site.gets.load(Ordering::SeqCst),0);
        let prefix=site.base.clone();let mut second=Page::launch(&json!({"cookies":[],"origins":[]}),None,true,Box::new(move|p|Ok(if p["request"]["url"].as_str().unwrap_or("").starts_with(&prefix){Decision::Continue}else{Decision::Abort}))).await.unwrap();
        second.navigate(&format!("{}/",site.base)).unwrap();assert_eq!(second.evaluate("document.cookie").unwrap(),"");
        drop(second);assert_eq!(first.body_text().unwrap(),"frozen");
    });
}
