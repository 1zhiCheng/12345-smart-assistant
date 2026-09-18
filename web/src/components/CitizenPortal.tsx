"use client";

import { useMemo, useState } from "react";
import { askCitizenQuestion, submitCitizenToOperator, transcribeCitizenAudio, type CitizenChatResult } from "@/lib/api";
import Icon from "./Icon";
import styles from "./CitizenPortal.module.css";

type Message = { role: "citizen" | "assistant"; text: string; result?: CitizenChatResult };

export default function CitizenPortal({ onBack }: { onBack: () => void }) {
  const [input, setInput] = useState("");
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [busy, setBusy] = useState(false);
  const [audioBusy, setAudioBusy] = useState(false);
  const [error, setError] = useState("");
  const [submitted, setSubmitted] = useState("");
  const [audioNote, setAudioNote] = useState("");
  const lastResult = useMemo(() => [...messages].reverse().find(message => message.result)?.result || null, [messages]);

  async function ask() {
    const query = input.trim();
    if (query.length < 2 || busy) return;
    setBusy(true); setError(""); setSubmitted(""); setInput("");
    setMessages(current => [...current, { role: "citizen", text: query }]);
    try {
      const result = await askCitizenQuestion(query, sessionId);
      setSessionId(result.session_id);
      setMessages(current => [...current, { role: "assistant", text: result.answer, result }]);
    } catch (err) { setError(err instanceof Error ? err.message : "咨询暂时不可用，请稍后重试。"); }
    finally { setBusy(false); }
  }

  async function handleAudio(file: File | undefined) {
    if (!file || audioBusy) return;
    setAudioBusy(true); setError(""); setAudioNote("");
    try {
      const result = await transcribeCitizenAudio(file);
      setInput(result.text);
      setAudioNote(result.generation.mode === "llm"
        ? "已本地转写并由大模型整理为咨询结构，请确认后发送咨询。"
        : `已本地转写并按规则整理，请确认后发送咨询。${result.generation.reason ? `（${result.generation.reason}）` : ""}`);
    } catch (err) { setError(err instanceof Error ? err.message : "语音转文字失败，请改用文字输入。"); }
    finally { setAudioBusy(false); }
  }

  async function submitToOperator() {
    const citizenMessages = messages.filter(message => message.role === "citizen");
    const latestCitizenText = citizenMessages.length ? citizenMessages[citizenMessages.length - 1].text : "";
    const content = input.trim() || latestCitizenText;
    if (content.length < 2) { setError("请先输入需要转交营业员的问题。"); return; }
    setBusy(true); setError("");
    try {
      const result = await submitCitizenToOperator(content, sessionId);
      setSubmitted(`已提交营业员审核，编号：${result.submission_id}。营业员将核实后决定是否生成正式工单。`);
    } catch (err) { setError(err instanceof Error ? err.message : "提交失败，请稍后重试。"); }
    finally { setBusy(false); }
  }

  return <div className={styles.page}>
    <header className={styles.header}><div className={styles.brand}><span>芜</span><div><b>芜湖 12345 市民服务</b><small>政策咨询 · 历史办件参考 · 转人工审核</small></div></div><button onClick={onBack}>工作人员入口</button></header>
    <main className={styles.main}>
      <section className={styles.hero}><span>WUHU CITIZEN SERVICE</span><h1>先自助咨询，必要时转交人工</h1><p>系统只引用芜湖官方文档与脱敏、已办结历史办件摘要；无法可靠回答时可直接提交给 12345 营业员审核。</p></section>
      <section className={styles.chat}>
        <div className={styles.notice}><Icon name="shield" size={16}/><span>请勿输入身份证号、手机号等敏感信息。语音仅用于本次转写，不保存原始录音。</span></div>
        <div className={styles.messages}>{messages.length === 0 ? <div className={styles.empty}><Icon name="chat" size={28}/><b>您好，请问有什么可以帮您？</b><span>例如：镜湖区夜间施工噪声应向哪个部门反映？</span></div> : messages.map((message, index) => <article className={message.role === "citizen" ? styles.citizen : styles.answer} key={index}><b>{message.role === "citizen" ? "您" : "芜湖智慧助手"}</b><p>{message.text}</p>{message.result && <><div className={styles.hint}>{message.result.review_hint}</div>{message.result.public_history.length > 0 && <details><summary>查看相似已办结事项参考（已脱敏）</summary>{message.result.public_history.map(item => <div className={styles.history} key={item.case_id}><b>{item.title}</b><small>{item.location || "地点未公开"} · {item.status}</small><p>{item.summary}</p>{item.reply_summary && <p>办理参考：{item.reply_summary}</p>}</div>)}</details>}{message.result.citations.length > 0 && <details><summary>查看官方文档依据（{message.result.citations.length} 条）</summary>{message.result.citations.map((item, citationIndex) => <div className={styles.history} key={`${item.doc_id}-${citationIndex}`}><b>{item.doc_title}</b><p>{item.snippet}</p></div>)}</details>}</>}</article>)}</div>
        <div className={styles.composer}><textarea value={input} onChange={event => setInput(event.target.value)} placeholder="请输入您要咨询或反映的事项…" disabled={busy || audioBusy}/><div><label className={styles.audio}><Icon name="upload" size={15}/>{audioBusy ? "正在转写与整理…" : "语音输入"}<input type="file" accept="audio/*,.m4a,.ogg" disabled={busy || audioBusy} onChange={event => void handleAudio(event.target.files?.[0])}/></label><button className={styles.handoff} disabled={busy} onClick={submitToOperator}>未解决，转营业员</button><button className={styles.send} disabled={busy || audioBusy || input.trim().length < 2} onClick={ask}>{busy ? "咨询中…" : "发送咨询"}</button></div></div>
        {audioNote && <div className={styles.submitted}>{audioNote}</div>}{error && <div className={styles.error}>{error}</div>}{submitted && <div className={styles.submitted}>{submitted}</div>}
        {lastResult?.requires_operator_review && <div className={styles.review}>该咨询涉及投诉、事实核验或依据不足，建议点击“未解决，转营业员”。</div>}
      </section>
    </main>
  </div>;
}
