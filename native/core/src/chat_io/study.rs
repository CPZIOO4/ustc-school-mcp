//! Bounded read-only study query; IDs always come from live school results.
use super::*;



pub(super) const INSTRUCTIONS: &str = "查未来七天课表、按课程名核实BB作业截止和提交状态、安排学习时，优先一次调用 school_study_query；它内部完成固定只读步骤，无需另建任务或取schema。用户给出的课程简称作为course_keyword，作业关键词使用标题中的短片段，例如期末项目用“期末”、Lab1用“Lab1”；学期如2026春季用2026SP。只改写、精简已有结果的追问直接使用对话中的事实，不重新查学校。学习安排回答默认控制在350字以内（网址不计），直接列出课程时间、作业截止与历史提交状态、空闲时段安排，不附加重复提醒或额外服务邀请。附件正文未读取时，安排只写复习对应课程或回看本人该作业；不要指定未经核实的软件、算法、题目、推导或他人的作业材料。保留完整的Markdown标题链接，网址不可省略成编号或省略号。历史已提交作业用于复习，不当作待交。未读取的附件只知道名称，不能推断题目内容。";

pub(super) fn schema() -> Value {
    json!({"type":"function","function":{"name":"school_study_query","description":"一次只读查询指定日期课表，并按课程、学期、作业关键词查找和核实BB作业。日期限今天到未来六天；重名返回候选，不猜ID，不创建提交尝试。","parameters":{"type":"object","additionalProperties":false,"required":["date","assignments"],"properties":{"date":{"type":"string","description":"YYYY-MM-DD；中国标准时间"},"assignments":{"type":"array","maxItems":4,"items":{"type":"object","additionalProperties":false,"required":["course_keyword","term","assignment_keyword"],"properties":{"course_keyword":{"type":"string","minLength":1,"maxLength":80},"term":{"type":"string","maxLength":30,"description":"如2026SP、2026FA；未指定学期用空串"},"assignment_keyword":{"type":"string","minLength":1,"maxLength":80}}}}}}}})
}
fn assignment_link(entry: &Value, label: &str) -> Option<Value> {
    let path=entry["entry_path"].as_str()?;
    if !path.starts_with("/webapps/assignment/uploadAssignment?") || path.contains(['\\','\r','\n']) {return None;}
    let url=url::Url::parse(&format!("https://www.bb.ustc.edu.cn{path}")).ok()?;
    let pairs:std::collections::HashMap<_,_>=url.query_pairs().collect();
    if pairs.get("course_id").map(|v|v.as_ref()) != entry["course_id"].as_str()
        || pairs.get("content_id").map(|v|v.as_ref()) != entry["content_id"].as_str()
        || pairs.get("mode").map(|v|v.as_ref()) != Some("view") {return None;}
    Some(json!({"label":label,"url":url.as_str(),"checked_at":chrono::Utc::now().to_rfc3339()}))
}
pub(super) fn resume_schema(name: &str) -> Value {
 json!({"type":"function","function":{"name":name,"description":"用当前对话的query_id恢复课表与作业分阶段查询；continue核实下一项历史，status查看已有结果。","parameters":{"type":"object","additionalProperties":false,"required":["query_id"],"properties":{"query_id":{"type":"string"}}}}})
}
impl Turn<'_> {
 fn study_save(&mut self, result: &Value) -> Result<()> {
  self.chat["study_snapshot"]=result.clone();
  if !self.chat["source_links"].is_array(){self.chat["source_links"]=json!([]);}
  for row in result["assignments"].as_array().into_iter().flatten() {
   if let Some(link)=assignment_link(&row["entry"],row["link"]["label"].as_str().unwrap_or("作业原页")) {
    let links=self.chat["source_links"].as_array_mut().unwrap();links.retain(|v|v["url"]!=link["url"]);links.push(link);if links.len()>20{links.remove(0);}
   }
  }
  self.save()?;self.notify();Ok(())
 }
 fn study_step(&mut self, input: Value) -> Result<Value> {
  let started=Instant::now();
  let result=self.legacy.call("study_flow",input,self.stopped,self.notify)?;
  self.timing("study",result["state"].as_str().unwrap_or("partial"),started,json!({}))?;
  self.study_save(&result)?;
  Ok(result)
 }
 pub(super) fn study_resume(&mut self, args: &Value, advance: bool) -> Result<Value> {
  if args["query_id"]!=self.chat["study_snapshot"]["query_id"] || !args["query_id"].is_string(){return Err(Error::invalid());}
  self.study_step(json!({"action":if advance {"continue"} else {"status"},"query_id":args["query_id"]}))
 }
 pub(super) fn study_query(&mut self, args: &Value) -> Result<Value> {
  for service in ["jw","bb"] {
   if service=="bb" && args["assignments"].as_array().is_some_and(|a|a.is_empty()){continue;}
   if !self.connected.contains(service) {
    let ready=self.legacy.call("connect",json!({"service":service}),self.stopped,self.notify)?;
    if let Some(problem)=recovery::returned_failure(&ready,Some(service)){return Ok(problem);}
    self.connected.insert(service.into());
   }
  }
  let mut input=args.clone();input["action"]=json!("start");
  let mut result=self.study_step(input)?;
  // The native UI renders this durable quick snapshot before each slow read.
  for _ in 0..4 {
   if self.stopped() || result["next_tool"].is_null() || result["step_running"]==true {break;}
   result=self.study_step(json!({"action":"continue","query_id":result["query_id"]}))?;
  }
  Ok(result)
 }
}
#[cfg(test)]
mod tests {
 use super::*;
 #[test] fn links_require_actual_assignment_identity_and_view_mode(){
  let mut v=json!({"course_id":"_1_1","content_id":"_2_1","entry_path":"/webapps/assignment/uploadAssignment?course_id=_1_1&content_id=_2_1&mode=view"});assert!(assignment_link(&v,"作业").is_some());v["content_id"]=json!("_3_1");assert!(assignment_link(&v,"作业").is_none());v["entry_path"]=json!("//evil.example/");assert!(assignment_link(&v,"作业").is_none());
 }
}
