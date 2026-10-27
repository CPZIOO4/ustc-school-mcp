use school_core::model;
use serde_json::{json,Value};
use std::{io::{Read,Write},net::TcpListener,thread,time::Duration};
#[test]
fn real_http_sse_arrives_before_request_finishes() {
    let listener=TcpListener::bind("127.0.0.1:0").unwrap();
    let url=format!("http://{}",listener.local_addr().unwrap());
    let worker=thread::spawn(move ||{
        let (mut socket,_)=listener.accept().unwrap();socket.set_read_timeout(Some(Duration::from_secs(10))).unwrap();
        let mut request=Vec::new();let mut b=[0;1024];
        loop {let n=socket.read(&mut b).unwrap();assert!(n>0);request.extend_from_slice(&b[..n]);if let Some(i)=request.windows(4).position(|v|v==b"\r\n\r\n"){
            let h=String::from_utf8_lossy(&request[..i]).to_lowercase();let len:usize=h.lines().find_map(|l|l.strip_prefix("content-length:")).unwrap().trim().parse().unwrap();
            if request.len()>=i+4+len {let body:Value=serde_json::from_slice(&request[i+4..]).unwrap();assert_eq!(body["stream"],true);break;}
        }}
        socket.write_all(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\n\r\n").unwrap();
        for text in ["正在","逐步","生成"] {let data=format!("data: {}\n\n",json!({"choices":[{"index":0,"delta":{"content":text},"finish_reason":null}]}));socket.write_all(data.as_bytes()).unwrap();socket.flush().unwrap();thread::sleep(Duration::from_millis(80));}
        let data=format!("data: {}\n\ndata: [DONE]\n\n",json!({"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}));socket.write_all(data.as_bytes()).unwrap();
    });
    let rt=tokio::runtime::Builder::new_current_thread().enable_all().build().unwrap();
    let mut metrics=json!({});
    let mut previews=Vec::new();let result=rt.block_on(async {let client=model::Client::new().unwrap();client.stream_measured(&json!({"settings":{"base_url":url,"model":"fixture","api_key":"synthetic"},"messages":[],"tools":[]}),&mut |text|previews.push(text.to_owned()), &mut metrics).await.unwrap()});
    worker.join().unwrap();assert_eq!(previews,vec!["正在","正在逐步","正在逐步生成"]);assert_eq!(result["content"],"正在逐步生成");
    assert!(metrics["total_ms"].as_u64().unwrap() >= metrics["first_content_ms"].as_u64().unwrap()+120);
    assert_eq!(metrics["http_status"],200);
    assert!(!metrics.to_string().contains("synthetic"));
}
