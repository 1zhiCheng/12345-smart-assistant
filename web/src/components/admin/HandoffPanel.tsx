"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  claimHandoff, completeHandoff, listHandoffs, recommendHandoff,
  type Dashboard, type HandoffRecommendation, type StandardWorkOrder, type User,
} from "@/lib/api";
import Icon from "../Icon";
import styles from "./admin.module.css";

type QueueStatus = "pending" | "processing" | "completed" | "all";

const STATUS_LABEL: Record<string, string> = {
  pending: "待部门领取", processing: "办理中", completed: "已办结", all: "全部工单",
};

function textField(value: unknown): string {
  return typeof value === "string" ? value : "";
}

export default function HandoffPanel({ data, user }: { data: Dashboard; user: User }) {
  const [status, setStatus] = useState<QueueStatus>("pending");
  const [rows, setRows] = useState<StandardWorkOrder[]>([]);
  const [selectedId, setSelectedId] = useState("");
  const [recommendation, setRecommendation] = useState<HandoffRecommendation | null>(null);
  const [category, setCategory] = useState("");
  const [deptId, setDeptId] = useState("");
  const [reply, setReply] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  const load = useCallback(async (nextStatus: QueueStatus = status) => {
    setLoading(true); setError("");
    try {
      const result = await listHandoffs(nextStatus);
      setRows(result);
      setSelectedId(current => result.some(row => row.case_id === current) ? current : (result[0]?.case_id || ""));
    } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
    finally { setLoading(false); }
  }, [status]);

  useEffect(() => { load(); }, [load]);
  const selected = useMemo(() => rows.find(row => row.case_id === selectedId) || null, [rows, selectedId]);

  useEffect(() => {
    if (!selected) return;
    const savedRecommendation = (selected as StandardWorkOrder & { b_recommendation?: HandoffRecommendation }).b_recommendation || null;
    const routing = selected.routing || {};
    const classification = selected.classification || {};
    const savedReply = selected.reply || {};
    setRecommendation(savedRecommendation);
    setCategory(savedRecommendation?.classification.category || textField(classification.category));
    setDeptId(savedRecommendation?.routing.dept_ids[0] || ((routing.dept_ids as string[] | undefined)?.[0] || ""));
    setReply(savedRecommendation?.reply.draft || textField(savedReply.draft) || textField(savedReply.content));
    setError("");
  }, [selected]);

  function replaceRow(next: StandardWorkOrder) {
    setRows(current => current.map(row => row.case_id === next.case_id ? next : row));
  }

  async function onClaim() {
    if (!selected) return;
    setLoading(true); setError("");
    try { replaceRow(await claimHandoff(selected.case_id)); }
    catch (e) { setError(e instanceof Error ? e.message : String(e)); }
    finally { setLoading(false); }
  }

  async function onRecommend() {
    if (!selected) return;
    setLoading(true); setError("");
    try {
      const next = await recommendHandoff(selected.case_id);
      setRecommendation(next); setCategory(next.classification.category);
      setDeptId(next.routing.dept_ids[0] || ""); setReply(next.reply.draft);
    } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
    finally { setLoading(false); }
  }

  async function onComplete() {
    if (!selected || !category.trim() || !deptId || !reply.trim()) {
      setError("分类、承办部门和审核后的回复内容不能为空。"); return;
    }
    setLoading(true); setError("");
    try {
      const dept = data.departments.find(item => item._id === deptId);
      const next = await completeHandoff(selected.case_id, {
        classification: { category: category.trim(), confidence: recommendation?.classification.confidence, reviewed_by: user.id },
        routing: { dept_ids: [deptId], dept_names: [dept?.name || deptId], matched_by: "human_review" },
        reply: {
          draft: reply.trim(), status: "reviewed", reviewed_by: user.id,
          citations: recommendation?.policy_basis.citations || [],
          verification: recommendation?.policy_basis.verification || {},
        },
      });
      replaceRow(next);
    } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
    finally { setLoading(false); }
  }

  return <div className={styles.panelStack}>
    <div className={styles.sectionHead}><div><span className={styles.eyebrow}>DEPARTMENT CASEWORK</span><h2>部门办理台：政策检索与回复审核</h2><p>这里不是工单终点，而是承办部门的待办列表：领取工单 → 检索本部门官方文档 → 生成办理建议与回复草稿 → 人工审核办结。</p></div><button className={styles.btnGhost} onClick={() => load()}><Icon name="refresh" size={15}/>刷新队列</button></div>
    {error && <div className={styles.errorBanner}><Icon name="shield" size={17}/>{error}</div>}
    <div className={styles.row}>{(["pending", "processing", "completed", "all"] as QueueStatus[]).map(value => <button key={value} className={value === status ? styles.btn : styles.btnGhost} onClick={() => { setStatus(value); load(value); }}>{STATUS_LABEL[value]}</button>)}</div>

    <div className={styles.handoffLayout}>
      <section className={styles.card}>
        <div className={styles.cardTitle}><span><Icon name="review" size={17}/>部门待办工单 · {rows.length}</span><em>{STATUS_LABEL[status]}</em></div>
        <div className={styles.handoffList}>{loading && rows.length === 0 ? <div className={styles.empty}>正在读取工单…</div> : rows.length === 0 ? <div className={styles.empty}>当前队列暂无工单</div> : rows.map(row => <button key={row.case_id} className={row.case_id === selectedId ? styles.handoffActive : ""} onClick={() => setSelectedId(row.case_id)}><span><b>{row.title}</b><small>{row.case_id} · {row.elements.location || "地点待补充"}</small></span><em className={`${styles.badge} ${row.handoff_status === "completed" ? styles.badgeGreen : row.handoff_status === "processing" ? styles.badgeBlue : styles.badgeAmber}`}>{STATUS_LABEL[row.handoff_status]}</em></button>)}</div>
      </section>

      <section className={styles.card}>
        {!selected ? <div className={styles.empty}>从左侧选择一张已确认工单</div> : <div className={styles.handoffDetail}>
          <div className={styles.cardTitle}><span>{selected.title}</span><em>{selected.case_id}</em></div>
          <div className={styles.grid2}>
            <div><span className={styles.fieldLabel}>事件经过</span><p>{selected.elements.event || "待补充"}</p></div>
            <div><span className={styles.fieldLabel}>群众诉求</span><p>{selected.elements.request || "待补充"}</p></div>
          </div>
          <div className={styles.row}>
            {selected.handoff_status === "pending" && <button className={styles.btn} disabled={loading} onClick={onClaim}>领取工单</button>}
            {selected.handoff_status !== "completed" && <button className={styles.btnGhost} disabled={loading} onClick={onRecommend}><Icon name="spark" size={14}/>{loading ? "正在检索部门文档…" : "检索本部门官方文档并生成办理建议"}</button>}
          </div>
          <div className={styles.handoffForm}>
            <label><span>事项分类</span><input className={styles.input} value={category} onChange={e => setCategory(e.target.value)} placeholder="如：生态环境" /></label>
            <label><span>承办部门</span><select className={styles.select} value={deptId} onChange={e => setDeptId(e.target.value)}><option value="">请选择部门</option>{data.departments.map(dept => <option key={dept._id} value={dept._id}>{dept.name}</option>)}</select></label>
          </div>
          <div className={styles.policyBox}>
            <div className={styles.cardTitle}><span><Icon name="database" size={16}/>政策依据</span><em>{recommendation ? `召回 ${recommendation.policy_basis.retrieved_count} 条` : "等待智能研判"}</em></div>
            {!recommendation?.policy_basis.citations.length ? <div className={styles.empty}>领取后点击“检索本部门官方文档并生成办理建议”，系统将在本部门知识库中召回政策依据。</div> : recommendation.policy_basis.citations.map((citation, index) => <div className={styles.policyCitation} key={`${citation.doc_id}-${citation.chunk_index}-${index}`}><b>{citation.doc_title}</b><small>{citation.section_path?.join(" / ") || `切片 ${citation.chunk_index}`}</small><p>{citation.snippet}</p></div>)}
          </div>
          {recommendation && <div className={`${styles.complianceGate} ${recommendation.release_gate.automated_passed ? styles.compliancePass : styles.complianceBlocked}`}>
            <div><b>{recommendation.release_gate.automated_passed ? "自动合规检查通过" : "回复草稿已被合规门禁拦截"}</b><small>得分 {(recommendation.release_gate.score * 100).toFixed(0)}% · 无论结果如何均须人工审核，不会自动发送</small></div>
            {recommendation.release_gate.issues.length > 0 && <ul>{recommendation.release_gate.issues.map(issue => <li key={issue}>{issue}</li>)}</ul>}
          </div>}
          <label className={styles.replyEditor}><span>办理建议与回复草稿（必须人工审核）</span><textarea className={styles.input} rows={7} value={reply} onChange={e => setReply(e.target.value)} placeholder="检索政策后生成办理建议与回复草稿，工作人员可在此修改" /></label>
          {selected.handoff_status === "processing" && <button className={styles.btn} disabled={loading} onClick={onComplete}>审核办理建议并提交办结</button>}
          {selected.handoff_status === "completed" && <div className={`${styles.badge} ${styles.badgeGreen}`}>已完成审核并回写处置结果</div>}
        </div>}
      </section>
    </div>
  </div>;
}
