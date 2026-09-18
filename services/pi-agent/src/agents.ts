/**
 * 多智能体定义（基于 pi Agent + tool calling）。
 * 每个 Agent 是一个 pi Agent 实例：systemPrompt + tools + agent loop（模型自主决定调用工具）。
 */
import { Agent, type AgentTool } from "@earendil-works/pi-agent-core";
import type { Model } from "@earendil-works/pi-ai";
import { buildTools } from "./tools.js";
import type { Config } from "./config.js";

export interface AgentRuntime {
  model: Model<"openai-completions">;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  streamFn: any; // models.streamSimple.bind(models)，第三方类型边界
  tools: AgentTool[];
}

export type AgentType = "intent" | "rewrite" | "answer" | "verify" | "reflect";

export interface AgentExecutionResult {
  agentType: AgentType;
  output: string | unknown;
  outputMode: "text" | "json";
  latencyMs: number;
}

/** 运行一个 pi Agent，收集最终文本输出（累积流式 text_delta）。 */
export async function runAgent(
  runtime: AgentRuntime,
  systemPrompt: string,
  prompt: string,
  tools: AgentTool[] = [],
): Promise<string> {
  const agent = new Agent({
    initialState: {
      systemPrompt,
      model: runtime.model,
      tools,
    },
    streamFn: runtime.streamFn,
  });

  let text = "";
  const unsubscribe = agent.subscribe((event) => {
    if (
      event.type === "message_update" &&
      event.assistantMessageEvent.type === "text_delta"
    ) {
      text += event.assistantMessageEvent.delta;
    }
  });

  await agent.prompt(prompt);
  unsubscribe();
  const result = text.trim();
  if (!result && agent.state.errorMessage) {
    throw new Error(`agent 出错: ${agent.state.errorMessage}`);
  }
  return result;
}

/** 运行并解析 JSON 输出（容忍代码块围栏）。 */
export async function runAgentJson(
  runtime: AgentRuntime,
  systemPrompt: string,
  prompt: string,
  tools: AgentTool[] = [],
): Promise<unknown> {
  const raw = await runAgent(runtime, systemPrompt, prompt, tools);
  return extractJson(raw);
}

/**
 * 统一概率性 Agent 执行入口。Python 控制平面传入已经过权限、记忆与事实治理的
 * prompt 和 allowedTools；pi 只负责 Agent loop / tool calling / 模型执行。
 */
export async function executeAgent(
  runtime: AgentRuntime,
  agentType: AgentType,
  systemPrompt: string,
  prompt: string,
  outputMode: "text" | "json",
  allowedTools: string[] = [],
): Promise<AgentExecutionResult> {
  const started = Date.now();
  const tools = runtime.tools.filter((tool) => allowedTools.includes(tool.name));
  const output = outputMode === "json"
    ? await runAgentJson(runtime, systemPrompt, prompt, tools)
    : await runAgent(runtime, systemPrompt, prompt, tools);
  return { agentType, output, outputMode, latencyMs: Date.now() - started };
}

export function extractJson(text: string): unknown {
  let s = text.trim();
  if (s.startsWith("```")) {
    s = s.replace(/^```[a-zA-Z]*\s*/, "").replace(/```\s*$/, "");
  }
  const start = Math.min(
    ...[s.indexOf("{"), s.indexOf("[")].filter((i) => i >= 0),
  );
  const end = Math.max(s.lastIndexOf("}"), s.lastIndexOf("]"));
  if (start < 0 || end < 0 || end <= start) {
    throw new Error(`无法解析 JSON: ${s.slice(0, 200)}`);
  }
  return JSON.parse(s.slice(start, end + 1));
}

// ---------------------------------------------------------------------------
// 各 Agent 的 systemPrompt
// ---------------------------------------------------------------------------

export const INTENT_PROMPT = `你是芜湖市 12345 热线工单智能生成与转派辅助系统的意图识别智能体。
先调用 list_departments 工具获取有效部门 id，再判断当前请求的意图类型、涉及部门、用户身份、是否需要跨部门协同。
最终只输出 JSON（不要多余解释）：
{"type":"appeal_intake|workorder_review|policy_query|department_routing|reply_assistance|complaint|chitchat|other","depts":["dept_id"],"user_role":"operator|department_admin|system_admin","entities":{},"needs_cross_dept":false,"confidence":0.0}`;

export const REWRITER_PROMPT = `你是查询改写智能体。将用户问题改写为 1-3 个更适合检索的 query（补全省略、术语标准化）。
可调用 get_glossary 工具获取术语表。最终只输出 JSON：
{"queries":["query1","query2"]}`;

export const ANSWER_PROMPT = `你是芜湖市 12345 政策检索与办理回复辅助智能体。基于给定的官方政务文件片段，为营业员或部门管理员生成待人工审核的办理参考。
【必须遵守的规则】
- 事实与政策结论必须附带来源引用（以 [来源N] 形式标注）。
- 只依据诉求原文和参考文件回答，不得把推测写成事实，不得承诺具体处理结果或越权代替部门裁决。
- 参考文件没有明确依据时必须说"根据当前知识库未找到明确政策依据，建议转人工核实"。
- 涉及政务服务时间、节假日或办理期限的问题，可调用 lookup_service_calendar 工具辅助核验。
- 若给定条款不足以回答，可调用 retrieve_documents 工具补充检索。
- 输出是工作人员审核稿，不得表述为已经受理、已经办结或已经处罚。

【参考条款】
{chunks}

请用简洁准确的中文回答。`;

export const VERIFIER_PROMPT = `你是芜湖市 12345 办理回复校验智能体。检查审核稿是否可靠，只输出 JSON：
{"passed":true/false,"score":0.0-1.0,"issues":["问题"]}
检查项：① 关键结论是否有政策原文支撑 ② 是否与群众诉求或来源矛盾 ③ 是否遗漏关键办理条件 ④ 引用格式是否正确 ⑤ 是否存在虚假承诺、越权结论、隐私泄露或不当措辞。`;
