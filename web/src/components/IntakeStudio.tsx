"use client";

import { useEffect, useMemo, useState } from "react";
import {
  analyzeAppeal,
  clarifyWorkOrder,
  confirmWorkOrder,
  splitWorkOrder,
  transcribeAudio,
  type StandardWorkOrder,
  type User,
  type WorkOrderElements,
} from "@/lib/api";
import styles from "./IntakeStudio.module.css";

interface Props { user: User; onLogout: () => void }

const SAMPLE = "市民反映昨晚23点镜湖区东方小区二期施工单位持续施工，噪声较大，影响居民休息，希望立即停止夜间施工并调查处理。";
const FIELD_NAMES: Record<string, string> = { time: "时间", location: "地点", event: "事件经过", request: "群众诉求" };
type ClarificationKey = "time" | "location" | "event" | "request" | "additional_details";

export default function IntakeStudio({ user, onLogout }: Props) {
  const [sourceType, setSourceType] = useState<"text" | "audio">("text");
  const [text, setText] = useState("");
  const [audioName, setAudioName] = useState<string | null>(null);
  const [audioFile, setAudioFile] = useState<File | null>(null);
  const [transcriptGeneration, setTranscriptGeneration] = useState<StandardWorkOrder["generation"] | null>(null);
  const [rawTranscript, setRawTranscript] = useState("");
  const [dialogueTurnCount, setDialogueTurnCount] = useState(0);
  const [roleFormatMode, setRoleFormatMode] = useState<"acoustic" | "heuristic" | "labeled" | "unsegmented" | null>(null);
  const [diarizationConfidence, setDiarizationConfidence] = useState(0);
  const [diarizationReason, setDiarizationReason] = useState("");
  const [workorder, setWorkorder] = useState<StandardWorkOrder | null>(null);
  const [loading, setLoading] = useState(false);
  const [transcribing, setTranscribing] = useState(false);
  const [error, setError] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const [clarificationAnswers, setClarificationAnswers] = useState<Partial<Record<ClarificationKey, string>>>({});
  const [selectedMatterIds, setSelectedMatterIds] = useState<string[]>([]);
  const [splitDrafts, setSplitDrafts] = useState<StandardWorkOrder[]>([]);
  const audioUrl = useMemo(() => audioFile ? URL.createObjectURL(audioFile) : "", [audioFile]);

  useEffect(() => () => { if (audioUrl) URL.revokeObjectURL(audioUrl); }, [audioUrl]);

  async function analyze() {
    setLoading(true); setError(""); setConfirmed(false);
    try {
      const result = await analyzeAppeal({ text, source_type: sourceType, audio_file_name: audioName, raw_transcript: sourceType === "audio" ? rawTranscript : null });
      setWorkorder(result);
      setSelectedMatterIds(result.matter_candidates.map(item => item.id));
      setSplitDrafts([]);
      setClarificationAnswers({});
    } catch (err) {
      setError(err instanceof Error ? err.message : "分析失败");
    } finally { setLoading(false); }
  }

  async function handleAudio(file: File | undefined) {
    setAudioName(file?.name || null);
    setAudioFile(file || null);
    setTranscriptGeneration(null);
    setDialogueTurnCount(0);
    setRoleFormatMode(null);
    setDiarizationConfidence(0);
    setDiarizationReason("");
    setError("");
    if (!file) return;
    setTranscribing(true);
    try {
      const result = await transcribeAudio(file);
      setText(result.text);
      setRawTranscript(result.raw_text);
      setDialogueTurnCount(result.turns.length);
      setRoleFormatMode(result.role_format_mode);
      setDiarizationConfidence(result.diarization_confidence || 0);
      setDiarizationReason(result.diarization_reason || "");
      setTranscriptGeneration(result.generation);
    } catch (err) {
      setError(`${err instanceof Error ? err.message : "语音转写失败"} 你仍可在下方人工补录或校对。`);
    } finally {
      setTranscribing(false);
    }
  }

  function updateElement(key: "time" | "location" | "event" | "request", value: string) {
    if (!workorder) return;
    setWorkorder({ ...workorder, elements: { ...workorder.elements, [key]: value, ...(key === "time" ? {time_basis: "stated" as const} : {}) } });
  }

  async function submitClarification() {
    if (!workorder) return;
    setLoading(true); setError("");
    try {
      const result = await clarifyWorkOrder(workorder, clarificationAnswers);
      setWorkorder(result);
      setSelectedMatterIds(result.matter_candidates.map(item => item.id));
      setSplitDrafts([]);
      setClarificationAnswers({});
    } catch (err) {
      setError(err instanceof Error ? err.message : "追问补充失败");
    } finally { setLoading(false); }
  }

  async function createSplitDrafts() {
    if (!workorder) return;
    setLoading(true); setError("");
    try {
      setSplitDrafts(await splitWorkOrder(workorder, selectedMatterIds));
    } catch (err) {
      setError(err instanceof Error ? err.message : "拆单失败");
    } finally { setLoading(false); }
  }

  async function confirm() {
    if (!workorder) return;
    setLoading(true); setError("");
    try {
      setWorkorder(await confirmWorkOrder(workorder));
      setConfirmed(true);
    } catch (err) {
      setError(err instanceof Error ? err.message : "确认失败");
    } finally { setLoading(false); }
  }

  return <div className={styles.shell}>
    <header className={styles.header}>
      <div className={styles.flagShade}/>
      <div className={styles.brand}><span className={styles.mark}>芜</span><span><b>芜湖智慧政务</b><small>12345 热线智能受理工作台</small></span></div>
      <div className={styles.user}><span>{user.name}</span><button onClick={onLogout}>退出</button></div>
    </header>

    <div className={styles.progress}>
      {["诉求输入", "要素提取", "工单审核", "分类转派"].map((name, index) => <div key={name} className={`${styles.step} ${index === 0 || (confirmed && index <= 2) ? styles.done : index <= (workorder ? (confirmed ? 3 : 2) : 0) ? styles.active : ""}`}><span>{index + 1}</span>{name}</div>)}
    </div>

    <main className={styles.main}>
      <section className={styles.card}>
        <h2>诉求受理</h2><p>支持群众来电转写或文本诉求，结果必须由工作人员审核。</p>
        <div className={styles.tabs}>
          <button className={sourceType === "text" ? styles.selected : ""} onClick={() => setSourceType("text")}>文本输入</button>
          <button className={sourceType === "audio" ? styles.selected : ""} onClick={() => setSourceType("audio")}>录音输入</button>
        </div>
        {sourceType === "audio" && <div className={styles.upload}><b>选择脱敏录音</b><input type="file" accept="audio/*,.m4a,.ogg" disabled={transcribing} onChange={e => void handleAudio(e.target.files?.[0])} /><small>{transcribing ? "正在识别并智能整理录音，请稍候…" : audioName ? `已选择：${audioName}。转写结果会自动填入下方，请务必人工校对。` : "支持 MP3、M4A、WAV、WEBM、OGG 和 MP4，最大 25 MB。"}</small>{audioUrl && <div style={{marginTop:12,padding:"10px 12px",border:"1px solid #d7e3df",borderRadius:10,background:"#fff"}}><span style={{display:"block",marginBottom:7,fontSize:10,fontWeight:700,color:"#38544d"}}>录音试听（可播放、暂停和拖动进度）</span><audio controls preload="metadata" src={audioUrl} style={{display:"block",width:"100%",height:36}} /></div>}</div>}
        <label className={styles.label}>{sourceType === "audio" ? "角色格式化转写（可人工校对）" : "群众诉求原文"}</label>
        {sourceType === "audio" && roleFormatMode && <div style={{margin:"0 0 8px",padding:"7px 9px",borderRadius:8,background:roleFormatMode === "unsegmented" || (roleFormatMode === "acoustic" && diarizationConfidence < .5) ? "#fff3dc" : "#e7f4ef",fontSize:10,color:roleFormatMode === "unsegmented" || (roleFormatMode === "acoustic" && diarizationConfidence < .5) ? "#805d1d" : "#176552"}}><b>{roleFormatMode === "unsegmented" ? "未检测到明确双人话轮" : `已区分接线员与群众 · ${dialogueTurnCount} 个话轮`}</b><span style={{marginLeft:6}}>{roleFormatMode === "unsegmented" ? `当前按群众单人陈述处理，请人工核对。${diarizationReason ? ` ${diarizationReason}` : ""}` : roleFormatMode === "acoustic" ? `CAM++ 声纹分离 · 置信度 ${Math.round(diarizationConfidence * 100)}%；生成工单只读取群众话轮，低置信度请人工核对。` : "已按文本话术区分；请人工核对角色。"}</span></div>}
        {sourceType === "audio" && transcriptGeneration && <div style={{margin:"0 0 8px",fontSize:10,color:transcriptGeneration.mode === "llm" ? "#176552" : "#805d1d"}}><b>{transcriptGeneration.mode === "llm" ? "大模型整理" : "规则降级整理"}</b>{transcriptGeneration.mode === "llm" ? ` · ${transcriptGeneration.model}` : ` · ${transcriptGeneration.fallback_reason}`}</div>}
        <textarea className={styles.textarea} value={text} onChange={e => setText(e.target.value)} disabled={transcribing} placeholder={sourceType === "audio" ? "选择录音后将按“接线员：”“群众：”格式转写；可在这里人工校正角色和文字。" : "请输入群众诉求，建议保留时间、地点、事件经过和希望的处理方式。"} />
        {sourceType === "audio" && rawTranscript && <details style={{margin:"10px 0 12px",padding:"10px",border:"1px solid #d7e3df",borderRadius:10,background:"#f8fbfa",fontSize:11}}><summary style={{cursor:"pointer",fontWeight:700,color:"#176b5c"}}>查看并核对原始转写</summary><p style={{color:"#7a8d87"}}>原始文本仅用于溯源，生成工单前请以录音和整理稿为准。</p><textarea readOnly value={rawTranscript} style={{width:"100%",minHeight:110,padding:9,border:"1px solid #dbe5e2",borderRadius:8,background:"#fff",lineHeight:1.6}} /></details>}
        <button className={styles.example} onClick={() => setText(SAMPLE)}>填入演示案例</button>
        <button className={styles.primary} disabled={loading || transcribing || text.trim().length < 2} onClick={analyze}>{transcribing ? "正在转写…" : loading ? "处理中…" : "分析诉求并生成工单"}</button>
        {error && <div className={styles.error}>{error}</div>}
      </section>

      <section className={styles.card}>
        {!workorder ? <div className={styles.empty}><div><strong>等待诉求输入</strong><span>系统将提取关键要素、发现缺失信息并生成标准工单。</span></div></div> : <>
          <div className={styles.caseHead}><div><h2>标准工单审核</h2><p>AI生成内容仅供参考，请核对原始诉求后确认。</p></div><span className={styles.caseId}>{workorder.case_id}</span></div>
          <div style={{display:"flex",alignItems:"center",gap:8,margin:"-4px 0 12px",padding:"8px 10px",borderRadius:9,fontSize:10,background:workorder.generation.mode === "llm" ? "#e7f4ef" : "#fff3dc",color:workorder.generation.mode === "llm" ? "#176552" : "#805d1d"}}>
            <b>{workorder.generation.mode === "llm" ? "大模型结构化生成" : "规则降级生成"}</b>
            <span>{workorder.generation.mode === "llm" ? `${workorder.generation.provider} · ${workorder.generation.model}` : workorder.generation.fallback_reason}</span>
          </div>
          <div className={styles.grid}>
            <label className={styles.wide}><span className={styles.label}>工单标题</span><input className={styles.input} value={workorder.title} onChange={e => setWorkorder({...workorder, title:e.target.value})}/></label>
            {(["time", "location", "event", "request"] as const).map(key => <label key={key} className={key === "event" || key === "request" ? styles.wide : ""}><span className={styles.label}>{FIELD_NAMES[key]}{key === "time" && workorder.elements.time_basis === "received_at" && <em style={{marginLeft:6,fontStyle:"normal",fontWeight:500,color:"#9a6b16"}}>系统按来电时间补全</em>}</span>{key === "event" || key === "request" ? <textarea className={styles.input} value={workorder.elements[key]} onChange={e => updateElement(key,e.target.value)} /> : <input className={styles.input} value={workorder.elements[key]} onChange={e => updateElement(key,e.target.value)} />}</label>)}
            <label className={styles.wide}><span className={styles.label}>标准工单正文</span><textarea className={styles.textarea} value={workorder.content} onChange={e => setWorkorder({...workorder, content:e.target.value})}/></label>
          </div>
          <div className={styles.quality}>{[["完整度",workorder.quality.completeness],["忠实度",workorder.quality.fidelity],["清晰度",workorder.quality.clarity],["综合",workorder.quality.overall]].map(([name,value]) => <div className={styles.metric} key={String(name)}><b>{Math.round(Number(value)*100)}</b><small>{name}</small></div>)}</div>
          <div className={styles.alerts}>{workorder.missing_fields.map(name => <span className={styles.alert} key={name}>缺少：{FIELD_NAMES[name] || name}</span>)}{workorder.ambiguities.map(item => <span className={styles.alert} key={item}>{item}</span>)}</div>
          {workorder.generation.mode === "llm" && Object.keys(workorder.generation.evidence).length > 0 && <details style={{margin:"10px 0",padding:"9px 11px",border:"1px solid #d7e3df",borderRadius:10,background:"#f8fbfa",fontSize:10}}><summary style={{cursor:"pointer",color:"#176b5c",fontWeight:700}}>查看模型引用的原文证据</summary>{Object.entries(workorder.generation.evidence).map(([field, quotes]) => <div key={field} style={{marginTop:8}}><b>{FIELD_NAMES[field] || field}</b>{quotes.map((quote, index) => <p key={`${field}-${index}`} style={{margin:"3px 0",color:"#536963",lineHeight:1.5}}>“{quote}”</p>)}</div>)}</details>}
          {workorder.multiple_matters && <div style={{margin:"12px 0",padding:12,border:"1px solid #b9d6cf",borderRadius:11,background:"#f3faf8"}}><b style={{display:"block",fontSize:11,color:"#176552"}}>检测到多个事项，请审核是否拆单</b><p style={{fontSize:9,color:"#647b75",margin:"5px 0 8px"}}>系统只生成候选草稿，不会自动转派；每张子工单仍需人工确认。</p>{workorder.matter_candidates.map(item => <label key={item.id} style={{display:"flex",alignItems:"flex-start",gap:7,padding:"7px 0",borderTop:"1px solid #ddeae6",fontSize:10}}><input type="checkbox" checked={selectedMatterIds.includes(item.id)} onChange={e => setSelectedMatterIds(e.target.checked ? [...selectedMatterIds,item.id] : selectedMatterIds.filter(id => id !== item.id))}/><span><b>{item.topic}</b><small style={{display:"block",marginTop:3,color:"#63766f"}}>原文依据：{item.evidence}</small></span></label>)}<button className={styles.secondary} style={{width:"100%",marginTop:8}} disabled={loading || selectedMatterIds.length < 2} onClick={createSplitDrafts}>生成 {selectedMatterIds.length} 张子工单草稿</button>{splitDrafts.length > 0 && <div style={{marginTop:8}}>{splitDrafts.map(draft => <div key={draft.case_id} style={{padding:"7px 9px",marginTop:5,borderRadius:8,background:"#fff",fontSize:9}}><b>{draft.matter_index}. {draft.title}</b><span style={{display:"block",color:"#71827d",marginTop:2}}>{draft.case_id} · 父工单 {draft.parent_case_id}</span></div>)}</div>}</div>}
          {workorder.clarification_questions.map(item => <div className={styles.question} key={item}>建议追问：{item}</div>)}
          {(workorder.missing_fields.length > 0 || workorder.ambiguities.length > 0) && <div style={{marginTop:12,padding:12,border:"1px solid #ead7aa",borderRadius:11,background:"#fffbf2"}}><b style={{display:"block",fontSize:11,color:"#76551c",marginBottom:7}}>追问补充并重新生成</b>{workorder.missing_fields.map(field => <label key={field} style={{display:"block",marginTop:7}}><span style={{display:"block",fontSize:9,color:"#6f6250",marginBottom:4}}>{FIELD_NAMES[field] || field}</span>{field === "event" || field === "request" ? <textarea className={styles.input} value={clarificationAnswers[field as ClarificationKey] || ""} onChange={e => setClarificationAnswers({...clarificationAnswers,[field]:e.target.value})} /> : <input className={styles.input} value={clarificationAnswers[field as ClarificationKey] || ""} onChange={e => setClarificationAnswers({...clarificationAnswers,[field]:e.target.value})} />}</label>)}{workorder.ambiguities.length > 0 && <label style={{display:"block",marginTop:7}}><span style={{display:"block",fontSize:9,color:"#6f6250",marginBottom:4}}>其他核实信息</span><textarea className={styles.input} value={clarificationAnswers.additional_details || ""} onChange={e => setClarificationAnswers({...clarificationAnswers,additional_details:e.target.value})} /></label>}<button className={styles.primary} style={{marginTop:10}} disabled={loading || !Object.values(clarificationAnswers).some(value => value?.trim())} onClick={submitClarification}>{loading ? "正在重新生成…" : "提交补充信息"}</button></div>}
          <div className={styles.confirm}><button className={styles.secondary} onClick={() => setWorkorder(null)}>重新分析</button><button className={styles.primary} disabled={loading} onClick={confirm}>人工确认工单</button></div>
          {confirmed && <div className={styles.success}>工单已确认并进入成员 B 待分派队列（状态：待领取）。后续分类、承办部门和回复结果会回写到同一工单。</div>}
        </>}
      </section>
    </main>
  </div>;
}
