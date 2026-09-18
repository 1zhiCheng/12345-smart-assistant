/** 后端 API 类型与调用封装（经 Next.js rewrites 代理到 Python 后端）。 */

// ==================== 类型 ====================
export interface Citation {
  doc_id: string;
  doc_title: string;
  dept_id: string;
  chunk_index: number;
  section_path: string[];
  snippet: string;
}

export interface DeptRoute {
  dept_ids: string[];
  dept_names: string[];
  matched_by: string;
  confidence: number;
  reasons: string[];
}

export interface ChatResult {
  session_id: string;
  answer: string;
  citations: Citation[];
  dept_ids: string[];
  confidence: number;
  intent_type: string;
  verification: { passed: boolean; score: number; issues: string[] };
  retrieved_count: number;
  route?: DeptRoute | null;
}

/** 历史会话摘要（“最近”边栏列表项）。 */
export interface SessionSummary {
  session_id: string;
  title: string;
  message_count: number;
  updated_at: string;
}

/** 会话历史中的单条消息。 */
export interface HistoryMessage {
  role: "user" | "assistant";
  content: string;
  citations?: Citation[];
  query?: string;
}

export interface Department {
  _id: string;
  name: string;
  name_en?: string;
  category?: string;
  admin_users?: string[];
  agent_config?: Record<string, unknown>;
  loop_phase?: string;
  review_stats?: { total: number; correct: number; accuracy: number };
  fade_out?: { achieved_at: string; accuracy: number; samples: number; reason: string } | null;
}

export interface User {
  id: string;
  username: string;
  name: string;
  role: "operator" | "department_admin" | "system_admin";
  dept_id: string;
}

export interface WorkOrderElements {
  time: string;
  time_basis: "stated" | "received_at";
  location: string;
  subjects: string[];
  event: string;
  request: string;
  contact_hint: string;
}

export interface StandardWorkOrder {
  case_id: string;
  title: string;
  summary: string;
  content: string;
  region: string;
  source: {
    type: "text" | "audio";
    raw_text: string;
    masked_text: string;
    audio_file_name?: string | null;
  };
  elements: WorkOrderElements;
  missing_fields: string[];
  ambiguities: string[];
  clarification_questions: string[];
  quality: {
    completeness: number;
    fidelity: number;
    clarity: number;
    overall: number;
    warnings: string[];
  };
  generation: {
    mode: "llm" | "rule_fallback";
    provider: string;
    model: string;
    fallback_reason: string;
    evidence: Record<string, string[]>;
  };
  multiple_matters: boolean;
  matter_candidates: Array<{ id: string; topic: string; description: string; evidence: string }>;
  parent_case_id?: string | null;
  matter_index?: number | null;
  status: "draft" | "confirmed";
  requires_human_review: boolean;
  created_at: string;
  confirmed_at?: string | null;
  confirmed_by?: string | null;
  handoff_status: "not_ready" | "pending" | "processing" | "completed";
  handed_off_at?: string | null;
  claimed_at?: string | null;
  claimed_by?: string | null;
  completed_at?: string | null;
  classification?: Record<string, unknown> | null;
  routing?: Record<string, unknown> | null;
  reply?: Record<string, unknown> | null;
}

export interface HandoffRecommendation {
  case_id: string;
  classification: { category: string; confidence: number; source: string; reasons: string[] };
  routing: DeptRoute & { top_k: Array<{ dept_id: string; dept_name: string; rank: number }> };
  policy_basis: {
    citations: Citation[];
    verification: { passed?: boolean; score?: number; issues?: string[] };
    retrieved_count: number;
  };
  reply: { draft: string; status: string; citations: Citation[]; confidence: number; compliance: ReplyCompliance };
  release_gate: ReplyCompliance;
  requires_human_review: boolean;
}

export interface ReplyCompliance {
  version: string;
  status: "review_required" | "blocked";
  automated_passed: boolean;
  can_submit_for_review: boolean;
  can_auto_publish: false;
  requires_human_review: true;
  score: number;
  checks: Array<{ name: string; passed: boolean; detail: string }>;
  issues: string[];
  fingerprint: string;
}

export interface PipelineStage {
  key: string;
  name: string;
  done: boolean;
  detail?: string;
}

export interface Document {
  _id: string;
  dept_id: string;
  title: string;
  doc_type: string;
  version?: string;
  status: string;
  effective_date?: string | null;
  tags?: string[];
  chunk_count?: number;
  vector_status?: string;
  applicable_scope?: string[];
  cross_refs?: string[];
  pipeline_stages?: PipelineStage[];
  source?: { file_name: string; uploaded_by: string; uploaded_at: string };
  created_at?: string;
  updated_at?: string;
  chunks?: unknown[];
}

export interface QAItem {
  question: string;
  expected: string;
  answer: string;
  citations: Citation[];
  confidence: number;
  verdict: string | null;
  correct: boolean | null;
  correction: string;
}

export interface ReviewOrder {
  _id: string;
  dept_id: string;
  doc_id: string;
  doc_title: string;
  status: string;
  qa_pairs: QAItem[];
  total: number;
  correct: number;
  accuracy: number | null;
  created_at: string;
  reviewed_at?: string | null;
  reviewed_by?: string | null;
}

export interface Skill {
  _id: string;
  name: string;
  dept_id?: string;
  scope?: string;
  trigger?: { intent_patterns?: string[]; confidence_threshold?: number };
  action?: Record<string, unknown>;
  metrics?: { trigger_count?: number; success_rate?: number; avg_latency_ms?: number };
  version?: number;
  status?: string;
  auto_generated?: boolean;
  confidence?: number;
  created_by?: string;
  created_at?: string;
  unique_rules?: string[];
  rubric_rules?: string[];
  description?: string;
  origin?: string;
  gray_percent?: number;
  replay?: { baseline_score?: number; candidate_score?: number; delta?: number; passed?: boolean };
}

export interface Hook {
  _id: string;
  name: string;
  scope?: string;
  trigger?: Record<string, unknown>;
  action?: Record<string, unknown>;
  confidence?: number;
  status?: string;
  auto_generated?: boolean;
}

export interface Rule {
  _id: string;
  name: string;
  scope?: string;
  content?: string;
  priority?: number;
  status?: string;
  auto_generated?: boolean;
}

export interface AgentInfo {
  _id: string;
  name: string;
  name_en?: string;
  model: string;
  temperature: number;
  agent_config: Record<string, unknown>;
  loop_phase: string;
  review_stats: { total: number; correct: number; accuracy: number };
  doc_count: number;
  skill_count: number;
  hook_count: number;
  hot_queries: { q: string; n: number }[];
  replicas: number;
}

export interface MemoryPlaneInsight {
  key: "working" | "episodic" | "user" | "organization" | "learning";
  name: string;
  count: number;
  detail: string;
  store: string;
}

export interface SystemInsights {
  memory_planes: MemoryPlaneInsight[];
  fact_plane: {
    documents: number;
    active_documents: number;
    chunks: number;
    relations: number;
    conflicts: number;
  };
  governance: { usage_records: number; stale_org_memory: number; pending_candidates: number };
  evolution: {
    strategy_versions: number;
    executions: number;
    experiments: Array<Record<string, unknown>>;
    proposals: Array<Record<string, unknown>>;
    treatment: number;
    control: number;
  };
  signals: Record<string, number>;
  recent_traces: Array<{
    id: string; query: string; latency_ms: number; success: boolean; intent: string; created_at: string;
  }>;
}

export interface UserMemoryItem {
  _id: string;
  key: string;
  value: unknown;
  category: string;
  source_type?: string;
  revision?: number;
  created_at?: string;
  expires_at?: string;
}

export interface Dashboard {
  loop_phase_global: string;
  loop_enabled: boolean;
  thresholds: Record<string, number>;
  departments: Array<
    Department & {
      doc_count: number;
      chunk_count: number;
      skill_count: number;
      conflict_count: number;
    }
  >;
  skills: Skill[];
  hooks: Hook[];
  rules: Rule[];
  feedback: { total: number; up: number; down: number; adoption_rate: number };
  trace_count: number;
  pending_review_count: number;
  test_question_count: number;
  runtime?: {
    mode: "desktop_demo" | "production_like";
    storage_mode: string;
    vector_backend: string;
    embedding_provider: string;
    embedding_model: string;
    uses_real_vectors: boolean;
    vector_count: number | null;
    official_chunk_count: number;
    desktop_vector_cache: { enabled: boolean; ready: boolean };
    async_worker_required: boolean;
  };
}

interface ApiResponse<T> {
  code: number;
  message: string;
  data: T;
}

// ==================== 鉴权 token 存取 ====================
const TOKEN_KEY = "wuhu_12345_token";

export function getToken(): string | null {
  if (typeof window === "undefined") return null;
  return window.localStorage.getItem(TOKEN_KEY);
}
export function setToken(token: string) {
  window.localStorage.setItem(TOKEN_KEY, token);
}
export function clearToken() {
  window.localStorage.removeItem(TOKEN_KEY);
}

// ==================== 请求封装 ====================
async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    ...(options.headers as Record<string, string> | undefined),
  };
  const token = getToken();
  if (token) headers["Authorization"] = `Bearer ${token}`;

  const resp = await fetch(path, { ...options, headers });
  let json: ApiResponse<T> | { detail?: unknown } = { code: -1, message: "请求失败", data: null as unknown as T };
  try {
    json = (await resp.json()) as ApiResponse<T>;
  } catch {
    /* ignore parse error */
  }
  if (!resp.ok) {
    const detail = (json as { detail?: unknown }).detail;
    if (typeof detail === "string") throw new Error(detail);
    if (detail && typeof detail === "object") {
      const structured = detail as { message?: unknown; issues?: unknown };
      const message = typeof structured.message === "string" ? structured.message : `HTTP ${resp.status}`;
      const issues = Array.isArray(structured.issues) ? structured.issues.filter(item => typeof item === "string") : [];
      throw new Error(issues.length ? `${message}：${issues.join("；")}` : message);
    }
    throw new Error(`HTTP ${resp.status}`);
  }
  if (json && typeof json === "object" && "code" in json && (json as ApiResponse<T>).code !== 0) {
    throw new Error((json as ApiResponse<T>).message || "请求失败");
  }
  return (json as ApiResponse<T>).data;
}

// ==================== 鉴权 ====================
export async function login(username: string, password: string): Promise<{ token: string; user: User }> {
  return request<{ token: string; user: User }>("/api/v1/auth/login", {
    method: "POST",
    body: JSON.stringify({ username, password }),
  });
}

export async function me(): Promise<User> {
  return request<User>("/api/v1/auth/me");
}

export async function register(username: string, password: string, name: string): Promise<{ token: string; user: User }> {
  return request<{ token: string; user: User }>("/api/v1/auth/register", {
    method: "POST",
    body: JSON.stringify({ username, password, name }),
  });
}

// ==================== 12345 诉求受理与工单生成 ====================
export async function analyzeAppeal(payload: {
  text: string;
  source_type: "text" | "audio";
  audio_file_name?: string | null;
  raw_transcript?: string | null;
}): Promise<StandardWorkOrder> {
  return request<StandardWorkOrder>("/api/v1/intake/analyze", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export async function clarifyWorkOrder(
  workorder: StandardWorkOrder,
  answers: Partial<Record<"time" | "location" | "event" | "request" | "additional_details", string>>
): Promise<StandardWorkOrder> {
  return request<StandardWorkOrder>("/api/v1/intake/clarify", {
    method: "POST",
    body: JSON.stringify({ workorder, answers }),
  });
}

export async function splitWorkOrder(workorder: StandardWorkOrder, candidateIds: string[]): Promise<StandardWorkOrder[]> {
  return request<StandardWorkOrder[]>("/api/v1/intake/split", {
    method: "POST",
    body: JSON.stringify({ workorder, candidate_ids: candidateIds }),
  });
}

export interface TranscriptionResult {
  text: string;
  formatted_text: string;
  citizen_text: string;
  turns: Array<{ role: "operator" | "citizen" | "unknown"; speaker: string; text: string }>;
  role_format_mode: "acoustic" | "heuristic" | "labeled" | "unsegmented";
  raw_text: string;
  file_name: string;
  provider: string;
  model: string;
  diarization_confidence: number;
  diarization_reason: string;
  generation: StandardWorkOrder["generation"];
}

export interface CitizenChatResult extends ChatResult {
  public_history: Array<{ case_id: string; title: string; summary: string; location: string; status: string; reply_summary: string }>;
  requires_operator_review: boolean;
  review_hint: string;
}

export interface CitizenSubmission {
  submission_id: string;
  text: string;
  status: "pending_operator_review" | "claimed";
  created_at: string;
  claimed_at?: string | null;
  claimed_by?: string | null;
}

async function publicRequest<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers as Record<string, string> | undefined) },
  });
  let payload: ApiResponse<T> | { detail?: unknown } = { code: -1, message: "请求失败", data: null as unknown as T };
  try { payload = await response.json() as ApiResponse<T>; } catch { /* ignore */ }
  if (!response.ok) {
    const detail = (payload as { detail?: unknown }).detail;
    if (typeof detail === "string") throw new Error(detail);
    if (detail && typeof detail === "object") {
      const structured = detail as { message?: unknown; issues?: unknown };
      const issues = Array.isArray(structured.issues) ? structured.issues.filter(item => typeof item === "string") : [];
      throw new Error(issues.length ? `${structured.message || "请求失败"}：${issues.join("；")}` : String(structured.message || "请求失败"));
    }
    throw new Error(`HTTP ${response.status}`);
  }
  if (!("code" in payload) || payload.code !== 0) throw new Error((payload as ApiResponse<T>).message || "请求失败");
  return (payload as ApiResponse<T>).data;
}

export async function askCitizenQuestion(query: string, sessionId: string | null): Promise<CitizenChatResult> {
  return publicRequest<CitizenChatResult>("/api/v1/citizen/chat", { method: "POST", body: JSON.stringify({ query, session_id: sessionId }) });
}

export interface CitizenTranscriptionResult {
  text: string;
  raw_text: string;
  provider: string;
  model: string;
  generation: { mode: "llm" | "rule_fallback"; reason: string; provider: string; model: string };
}

export async function transcribeCitizenAudio(file: File): Promise<CitizenTranscriptionResult> {
  const form = new FormData(); form.append("file", file);
  let response: Response;
  try { response = await fetch("/api/v1/citizen/transcribe", { method: "POST", body: form }); }
  catch { throw new Error("语音上传连接中断，请重试或改用文字输入。"); }
  const payload = await response.json() as ApiResponse<CitizenTranscriptionResult> | { detail?: string };
  if (!response.ok) throw new Error((payload as { detail?: string }).detail || `HTTP ${response.status}`);
  return (payload as ApiResponse<CitizenTranscriptionResult>).data;
}

export async function submitCitizenToOperator(text: string, sessionId: string | null): Promise<{ submission_id: string; status: string }> {
  return publicRequest<{ submission_id: string; status: string }>("/api/v1/citizen/submissions", { method: "POST", body: JSON.stringify({ text, session_id: sessionId }) });
}

export async function listCitizenSubmissions(): Promise<CitizenSubmission[]> {
  return request<CitizenSubmission[]>("/api/v1/citizen/submissions");
}

export async function claimCitizenSubmission(id: string): Promise<CitizenSubmission> {
  return request<CitizenSubmission>(`/api/v1/citizen/submissions/${id}/claim`, { method: "POST" });
}

export async function transcribeAudio(file: File): Promise<TranscriptionResult> {
  const form = new FormData();
  form.append("file", file);
  const headers: Record<string, string> = {};
  const token = getToken();
  if (token) headers["Authorization"] = `Bearer ${token}`;
  let resp: Response;
  try {
    resp = await fetch("/api/v1/intake/transcribe", { method: "POST", headers, body: form });
  } catch {
    throw new Error("录音上传或转写连接中断。长录音在本机 CPU 上可能需要数分钟，请保持系统运行后重试；也可先在下方人工补录。");
  }
  let json: ApiResponse<TranscriptionResult> | { detail?: string };
  try {
    json = (await resp.json()) as ApiResponse<TranscriptionResult> | { detail?: string };
  } catch {
    throw new Error(`语音转写服务响应异常（HTTP ${resp.status}），请重试或查看启动日志。`);
  }
  if (!resp.ok) throw new Error((json as { detail?: string }).detail || `HTTP ${resp.status}`);
  if (!("code" in json) || json.code !== 0) throw new Error("语音转写失败");
  return json.data;
}

export async function confirmWorkOrder(workorder: StandardWorkOrder): Promise<StandardWorkOrder> {
  return request<StandardWorkOrder>("/api/v1/intake/confirm", {
    method: "POST",
    body: JSON.stringify({ workorder }),
  });
}

export async function listWorkOrders(): Promise<StandardWorkOrder[]> {
  return request<StandardWorkOrder[]>("/api/v1/intake/workorders");
}

export async function listHandoffs(status: "pending" | "processing" | "completed" | "all" = "pending"): Promise<StandardWorkOrder[]> {
  return request<StandardWorkOrder[]>(`/api/v1/intake/handoffs?status=${status}`);
}

export async function claimHandoff(caseId: string): Promise<StandardWorkOrder> {
  return request<StandardWorkOrder>(`/api/v1/intake/handoffs/${caseId}/claim`, { method: "POST" });
}

export async function recommendHandoff(caseId: string): Promise<HandoffRecommendation> {
  return request<HandoffRecommendation>(`/api/v1/intake/handoffs/${caseId}/recommend`, { method: "POST" });
}

export async function completeHandoff(
  caseId: string,
  result: { classification: Record<string, unknown>; routing: Record<string, unknown>; reply?: Record<string, unknown> | null }
): Promise<StandardWorkOrder> {
  return request<StandardWorkOrder>(`/api/v1/intake/handoffs/${caseId}/result`, {
    method: "POST",
    body: JSON.stringify(result),
  });
}

// ==================== 问答 ====================
export async function listDepartments(): Promise<Department[]> {
  return request<Department[]>("/api/v1/departments");
}

export async function askQuestion(payload: {
  query: string;
  session_id: string | null;
  dept_ids: string[] | null;
}): Promise<ChatResult> {
  return request<ChatResult>("/api/v1/chat", {
    method: "POST",
    body: JSON.stringify({
      query: payload.query,
      session_id: payload.session_id,
      dept_ids: payload.dept_ids,
    }),
  });
}

/** 提交显式反馈（点赞 👍 / 点踩 👎 / 纠错 correction），计入回答采纳率。 */
export async function submitFeedback(payload: {
  session_id: string;
  query: string;
  answer: string;
  signal: "up" | "down" | "correction";
}): Promise<{ received: string }> {
  return request<{ received: string }>("/api/v1/feedback", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export async function submitImplicitFeedback(payload: {
  session_id: string;
  signal: "copy" | "follow_up" | "abandon";
  query?: string;
  answer?: string;
}): Promise<{ received: string }> {
  return request<{ received: string }>("/api/v1/feedback/implicit", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

// ==================== 会话历史（“最近”边栏） ====================
/** 列出某用户的历史会话（按最近活跃时间倒序）。 */
export async function listSessions(): Promise<SessionSummary[]> {
  return request<SessionSummary[]>("/api/v1/chat/sessions");
}

/** 读取某会话的完整消息历史。 */
export async function sessionHistory(sessionId: string): Promise<HistoryMessage[]> {
  return request<HistoryMessage[]>(`/api/v1/chat/${sessionId}/history`);
}

/** 删除某会话（同时清除工作记忆与持久 trace）。 */
export async function deleteSession(sessionId: string): Promise<{ cleared: string }> {
  return request<{ cleared: string }>(`/api/v1/chat/${sessionId}`, { method: "DELETE" });
}

// ==================== 文档 ====================
export async function listDocuments(deptId?: string, status?: string): Promise<Document[]> {
  const q = new URLSearchParams();
  if (deptId) q.set("dept_id", deptId);
  if (status) q.set("status", status);
  const qs = q.toString();
  return request<Document[]>(`/api/v1/documents${qs ? "?" + qs : ""}`);
}

export async function getDocument(id: string): Promise<Document> {
  return request<Document>(`/api/v1/documents/${id}`);
}

export async function uploadDocument(
  file: File,
  deptId: string,
  uploadedBy = "admin"
): Promise<{ queued: boolean; job_id: string; file_name: string }> {
  const form = new FormData();
  form.append("file", file);
  form.append("dept_id", deptId);
  form.append("uploaded_by", uploadedBy);
  const headers: Record<string, string> = {};
  const token = getToken();
  if (token) headers["Authorization"] = `Bearer ${token}`;
  const resp = await fetch("/api/v1/documents/upload", { method: "POST", headers, body: form });
  const json = (await resp.json()) as ApiResponse<{ queued: boolean; job_id: string; file_name: string }>;
  if (!resp.ok || json.code !== 0) throw new Error(json.message || "上传失败");
  return json.data;
}

export interface AsyncJob {
  _id: string;
  type: string;
  status: "queued" | "running" | "completed" | "failed";
  result?: { document_id?: string; relations?: number; review_id?: string; error?: string };
}

export interface LoopCycleReport {
  cycle_id: string;
  started_at: string;
  completed_at: string;
  duration_ms: number;
  observed: number;
  bad_cases: number;
  signals: Record<string, number>;
  reflect: { root_causes?: Record<string, number>; suggestions?: Array<Record<string, unknown>> };
  adaptations: Array<{ type?: string; id?: string; name?: string; dept_id?: string; auto_activated?: boolean }>;
  deployed: { skills?: number; hooks?: number; rules?: number; rolled_back?: number };
  before: Record<string, number>;
  after: Record<string, number>;
  changes: Record<string, number>;
  summary: string;
  next_action: string;
  memory_retention?: Record<string, number>;
}

export interface LoopJob {
  _id: string;
  type: "run_loop";
  status: "queued" | "running" | "completed" | "failed";
  progress?: { stage?: string; detail?: string };
  result?: LoopCycleReport | { error?: string };
  created_at: string;
  updated_at: string;
}

export async function getUploadJob(jobId: string): Promise<AsyncJob> {
  return request<AsyncJob>(`/api/v1/documents/jobs/${jobId}`);
}

export async function updateDocumentStatus(id: string, status: string): Promise<{ doc_id: string; status: string }> {
  return request(`/api/v1/documents/${id}/status`, {
    method: "POST",
    body: JSON.stringify({ status }),
  });
}

// ==================== 管理端 ====================
export async function dashboard(): Promise<Dashboard> {
  return request<Dashboard>("/api/v1/admin/dashboard");
}

export async function agents(): Promise<AgentInfo[]> {
  return request<AgentInfo[]>("/api/v1/admin/agents");
}

export async function listReviewOrders(deptId?: string, status?: string): Promise<ReviewOrder[]> {
  const q = new URLSearchParams();
  if (deptId) q.set("dept_id", deptId);
  if (status) q.set("status", status);
  const qs = q.toString();
  return request<ReviewOrder[]>(`/api/v1/admin/review/orders${qs ? "?" + qs : ""}`);
}

export async function getReviewOrder(id: string): Promise<ReviewOrder> {
  return request<ReviewOrder>(`/api/v1/admin/review/orders/${id}`);
}

export async function submitReview(
  id: string,
  verdicts: { index: number; correct: boolean; correction: string }[]
): Promise<ReviewOrder> {
  return request<ReviewOrder>(`/api/v1/admin/review/orders/${id}/submit`, {
    method: "POST",
    body: JSON.stringify({ verdicts }),
  });
}

export async function generateReview(docId: string): Promise<ReviewOrder | null> {
  return request<ReviewOrder | null>(`/api/v1/admin/documents/${docId}/review`, { method: "POST" });
}

export async function listSkills(status?: string): Promise<Skill[]> {
  const q = status ? `?status=${encodeURIComponent(status)}` : "";
  return request<Skill[]>(`/api/v1/admin/skills${q}`);
}

export async function approveSkill(id: string): Promise<Skill> {
  return request<Skill>(`/api/v1/admin/skills/${id}/approve`, { method: "POST" });
}

export async function listHooks(): Promise<Hook[]> {
  return request<Hook[]>("/api/v1/admin/hooks");
}

export async function listRules(): Promise<Rule[]> {
  return request<Rule[]>("/api/v1/admin/rules");
}

export async function runLoop(): Promise<{ queued: boolean; job_id: string; status: string; message: string }> {
  return request<{ queued: boolean; job_id: string; status: string; message: string }>("/api/v1/admin/loop/run", { method: "POST" });
}

export async function getLoopJob(jobId: string): Promise<LoopJob> {
  return request<LoopJob>(`/api/v1/admin/loop/jobs/${jobId}`);
}

export async function listLoopJobs(limit = 10): Promise<LoopJob[]> {
  return request<LoopJob[]>(`/api/v1/admin/loop/jobs?limit=${limit}`);
}

export async function loopStats(): Promise<Record<string, unknown>> {
  return request<Record<string, unknown>>("/api/v1/admin/loop/stats");
}

export async function pendingFeedback(): Promise<Record<string, unknown>[]> {
  return request<Record<string, unknown>[]>("/api/v1/admin/feedback/pending");
}

export async function listTraces(limit = 50): Promise<Record<string, unknown>[]> {
  return request<Record<string, unknown>[]>(`/api/v1/admin/traces?limit=${limit}`);
}

export async function systemInsights(): Promise<SystemInsights> {
  return request<SystemInsights>("/api/v1/admin/system-insights");
}

export async function setLoopPhase(phase: string): Promise<{ loop_phase: string }> {
  return request<{ loop_phase: string }>("/api/v1/admin/loop/phase", {
    method: "POST",
    body: JSON.stringify({ phase }),
  });
}

export async function createDepartment(payload: {
  id: string;
  name: string;
  name_en?: string;
  category?: string;
  admin_users?: string[];
}): Promise<Department> {
  return request<Department>("/api/v1/departments", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export async function myMemories(): Promise<UserMemoryItem[]> {
  return request<UserMemoryItem[]>("/api/v1/memory/me");
}

export async function rememberForMe(payload: {
  key: string; value: unknown; category?: string; consent?: boolean;
}): Promise<UserMemoryItem> {
  return request<UserMemoryItem>("/api/v1/memory/me", {
    method: "POST",
    body: JSON.stringify({ ...payload, consent: payload.consent ?? true }),
  });
}

export async function forgetMemory(id: string): Promise<{ deleted: string }> {
  return request<{ deleted: string }>(`/api/v1/memory/me/${id}`, { method: "DELETE" });
}

export async function checkHealth(): Promise<boolean> {
  try {
    const resp = await fetch("/healthz");
    return resp.ok;
  } catch {
    return false;
  }
}
