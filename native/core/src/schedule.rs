//! Personal calendar: encrypted storage, idempotent batches and durable in-app reminders.
use crate::{sessions, storage, Error, Result};
use chrono::{DateTime, Utc, Duration};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::{path::Path, sync::Mutex};
static GATE: Mutex<()> = Mutex::new(());
pub const INSTRUCTIONS: &str = "个人日程使用 school_schedule，直接在应用内创建，不导出文件或调用学校网站。创建须有本轮办理许可。根据用户指定日期、时间和既有事实填写，ISO时间必须带+08:00。备注保留完整原始作业链接；历史作业用于复习。返回created/already_exists才可说已加入日程。提醒为本机应用内提醒：应用运行时到点显示，关闭后重开在日程结束前补提醒。简短说明日期、时段、提醒时间，界面会提供查看日程按钮。";
pub fn schema()->Value { json!({"type":"function","function":{"name":"school_schedule","description":"查询或创建本人应用内日程及提醒；create原子保存整批，重复请求不重复创建。","parameters":{"type":"object","properties":{"action":{"type":"string","enum":["list","create"]},"events":{"type":"array","minItems":1,"maxItems":20,"items":{"type":"object","properties":{"title":{"type":"string"},"start":{"type":"string","description":"RFC3339，例如2026-10-23T09:00:00+08:00"},"end":{"type":"string"},"reminder_minutes":{"type":"integer","minimum":0,"maximum":10080},"notes":{"type":"string"},"links":{"type":"array","items":{"type":"object","properties":{"title":{"type":"string"},"url":{"type":"string"}},"required":["title","url"],"additionalProperties":false}}},"required":["title","start","end","reminder_minutes","notes","links"],"additionalProperties":false}}},"required":["action"],"additionalProperties":false}}}) }
fn bad(s:&str)->Error { Error::new("invalid_arguments",s) }
fn date(v:&Value)->Result<DateTime<Utc>> { DateTime::parse_from_rfc3339(v.as_str().ok_or_else(Error::invalid)?).map(|x|x.with_timezone(&Utc)).map_err(|_|bad("日程时间需要包含时区。")) }
fn text(v:&Value,max:usize)->Result<String> { let s=v.as_str().ok_or_else(Error::invalid)?.trim();if s.chars().count()>max||s.chars().any(|c|c.is_control()&&c!='\n'){return Err(Error::invalid());}Ok(s.into()) }
fn normalized(v:&Value,now:DateTime<Utc>)->Result<Value> {
 let title=text(&v["title"],120)?;let notes=text(&v["notes"],4000)?;let start=date(&v["start"])?;let end=date(&v["end"])?;
 let minutes=v["reminder_minutes"].as_i64().filter(|n|(0..=10080).contains(n)).ok_or_else(Error::invalid)?;
 if title.is_empty()||start<=now||end<=start||end-start>Duration::days(7){return Err(bad("请填写未来的开始时间、晚于开始的结束时间；单项日程最长7天。"));}
 let links=v["links"].as_array().filter(|a|a.len()<=10).ok_or_else(Error::invalid)?;
 let mut clean=vec![];for l in links {let url=text(&l["url"],2048)?;let u=url::Url::parse(&url).map_err(|_|Error::invalid())?;if u.scheme()!="https"||!u.username().is_empty()||u.password().is_some()||u.host_str().is_none(){return Err(bad("日程链接须为完整HTTPS地址。"));}clean.push(json!({"title":text(&l["title"],180)?,"url":url}));}
 let mut e=json!({"title":title,"start":start.to_rfc3339(),"end":end.to_rfc3339(),"reminder_minutes":minutes,"reminder_at":(start-Duration::minutes(minutes)).to_rfc3339(),"notes":notes,"links":clean});
 let id=format!("{:x}",Sha256::digest(e.to_string().as_bytes()));e["id"]=json!(id);e["status"]=json!("active");e["reminder_state"]=json!("pending");e["created_at"]=json!(now.to_rfc3339());Ok(e)
}
fn tick(state:&mut Value,now:DateTime<Utc>)->Result<bool>{let mut changed=false;for e in state["events"].as_array_mut().ok_or_else(Error::invalid)? {if e["status"]=="active"&&e["reminder_state"]=="pending"&&date(&e["reminder_at"])?<=now {e["reminder_state"]=json!(if date(&e["end"])?<=now{"expired"}else{"unread"});e["reminded_at"]=json!(now.to_rfc3339());changed=true;}}Ok(changed)}
pub fn dispatch(root:&Path,input:&Value,authorized:bool)->Result<Value>{
 if root.join("locked").exists(){return Err(bad("请先登录学校身份。"));}
 let identity=sessions::dispatch("sessions_read",&json!({"profile":"identity","path":root.join("profile/ustc-identity.credentials.dpapi")}))?;
 let account=identity["username"].as_str().ok_or_else(Error::invalid)?;
 dispatch_owned(root,account,input,authorized,Utc::now())
}
fn dispatch_owned(root:&Path,account:&str,input:&Value,authorized:bool,now:DateTime<Utc>)->Result<Value>{
 let _lock=GATE.lock().map_err(|_|Error::invalid())?;
 let owner=format!("{:x}",Sha256::digest(account.as_bytes()));let path=root.join("profile/personal-calendar.dpapi");
 let mut state=storage::private_read(&json!({"path":path,"default":{"owner":owner,"events":[]}}))?;
 if state["owner"]!=owner{return Err(bad("日程属于另一个账号，请先切换到原账号。"));}
 let action=input["action"].as_str().ok_or_else(Error::invalid)?;
 if action!="list"&&!authorized{return Err(Error::new("authorization_required","请允许办理本条指令中的日程操作。"));}
 let mut receipts=vec![];let mut changed=tick(&mut state,now)?;
 match action {
 "list"=>{},
 "create"=>{let batch=input["events"].as_array().filter(|a|!a.is_empty()&&a.len()<=20).ok_or_else(Error::invalid)?;
 let items=batch.iter().map(|v|normalized(v,now)).collect::<Result<Vec<_>>>()?;
 let events=state["events"].as_array_mut().ok_or_else(Error::invalid)?;
 if events.len()+items.len()>1000{return Err(bad("日程已达本机数量上限。"));}
 for mut e in items{if let Some(old)=events.iter().find(|x|x["id"]==e["id"]){if old["status"]=="cancelled"{return Err(bad("同一日程已取消，请调整后重新创建。"));}let mut r=old.clone();r["result"]=json!("already_exists");receipts.push(r);}else{events.push(e.clone());e["result"]=json!("created");receipts.push(e);changed=true;}}
 },
 "cancel"|"ack"=>{let id=input["id"].as_str().ok_or_else(Error::invalid)?;let e=state["events"].as_array_mut().ok_or_else(Error::invalid)?.iter_mut().find(|e|e["id"]==id).ok_or_else(||bad("未找到这项日程。"))?;
 if action=="cancel"{e["status"]=json!("cancelled");e["reminder_state"]=json!("cancelled");}else if e["reminder_state"]=="unread"{e["reminder_state"]=json!("read");}changed=true;},
 _=>return Err(Error::invalid())}
 if changed{storage::private_write(&json!({"path":path,"value":state}))?;}
 let events:Vec<_>=state["events"].as_array().ok_or_else(Error::invalid)?.iter().filter(|e|e["status"]=="active").cloned().collect();
 Ok(json!({"state":if action=="create"{"created"}else{"ok"},"receipts":receipts,"events":events,"timezone":"Asia/Shanghai","delivery":"应用内提醒：运行时到点显示；重开应用后，未结束的日程补提醒。"}))
}
#[cfg(test)] mod tests {
 use super::*;
 fn item()->Value{json!({"title":"多元统计复习","start":"2030-01-02T09:00:00+08:00","end":"2030-01-02T10:25:00+08:00","reminder_minutes":10,"notes":"历史已提交作业复习","links":[{"title":"Lab1","url":"https://www.bb.ustc.edu.cn/webapps/assignment/uploadAssignment?mode=view&id=1"}]})}
 #[test] fn validation(){let now=DateTime::parse_from_rfc3339("2030-01-01T00:00:00Z").unwrap().with_timezone(&Utc);let e=normalized(&item(),now).unwrap();assert_eq!(e["reminder_at"],"2030-01-02T00:50:00+00:00");let mut v=item();v["end"]=v["start"].clone();assert!(normalized(&v,now).is_err());v=item();v["links"][0]["url"]=json!("javascript:alert(1)");assert!(normalized(&v,now).is_err());assert!(normalized(&item(),now+Duration::days(2)).is_err());}
 #[test] fn durable_idempotent_and_due(){let root=std::env::temp_dir().join(format!("school-calendar-test-{}",uuid::Uuid::new_v4()));std::fs::create_dir_all(root.join("profile")).unwrap();let now=DateTime::parse_from_rfc3339("2030-01-01T00:00:00Z").unwrap().with_timezone(&Utc);let req=json!({"action":"create","events":[item()]});assert!(dispatch_owned(&root,"test",&req,false,now).is_err());let first=dispatch_owned(&root,"test",&req,true,now).unwrap();let second=dispatch_owned(&root,"test",&req,true,now).unwrap();assert_eq!(first["events"],second["events"]);assert_eq!(second["receipts"][0]["result"],"already_exists");let due=date(&first["events"][0]["reminder_at"]).unwrap();let x=dispatch_owned(&root,"test",&json!({"action":"list"}),false,due).unwrap();assert_eq!(x["events"][0]["reminder_state"],"unread");let x2=dispatch_owned(&root,"test",&json!({"action":"list"}),false,due+Duration::minutes(1)).unwrap();assert_eq!(x["events"][0]["reminded_at"],x2["events"][0]["reminded_at"]);assert!(dispatch_owned(&root,"other",&json!({"action":"list"}),false,now).is_err());dispatch_owned(&root,"test",&json!({"action":"ack","id":x["events"][0]["id"]}),true,due).unwrap();assert_eq!(dispatch_owned(&root,"test",&json!({"action":"list"}),false,due).unwrap()["events"][0]["reminder_state"],"read");std::fs::remove_file(root.join("profile/personal-calendar.dpapi")).unwrap();std::fs::remove_dir(root.join("profile")).unwrap();std::fs::remove_dir(root).unwrap();}
}
