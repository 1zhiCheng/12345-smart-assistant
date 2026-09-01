"use client";

import { useState } from "react";
import { login, register, setToken, type User } from "@/lib/api";
import Icon from "./Icon";
import styles from "./Login.module.css";

const DEMOS = [
  { type: "营业员", user: "operator", pass: "operator123", desc: "诉求录入、要素确认与工单生成", icon: "chat" as const },
  { type: "部门管理员", user: "cgj_admin", pass: "admin123", desc: "本部门知识、转派审核与回复管理", icon: "building" as const },
  { type: "系统管理员", user: "admin", pass: "admin123", desc: "全局账号、知识库与策略治理", icon: "shield" as const },
];

export default function Login({ onLogin }: { onLogin: (user: User) => void }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [name, setName] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [mode, setMode] = useState<"login" | "register">("login");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (busy) return;
    if (mode === "register" && password !== confirmPassword) { setError("两次输入的密码不一致"); return; }
    setError(""); setBusy(true);
    try {
      const res = mode === "login"
        ? await login(username.trim(), password)
        : await register(username.trim(), password, name.trim());
      setToken(res.token); onLogin(res.user);
    } catch (err) { setError(String(err instanceof Error ? err.message : err)); }
    finally { setBusy(false); }
  }

  return (
    <div className={styles.wrap}>
      <header className={styles.flagHeader}>
        <div className={styles.flagShade} />
        <div className={styles.govBrand}>
          <span className={styles.govIcon}><Icon name="building" size={27}/></span>
          <span><b>芜湖智慧政务</b><small>WUHU SMART GOVERNMENT</small></span>
        </div>
        <div className={styles.headerMotto}>为民 · 便民 · 惠民</div>
      </header>
      <div className={styles.cityBackdrop} />
      <div className={styles.ambientOne} /><div className={styles.ambientTwo} />
      <main className={styles.stage}>
      <section className={styles.story}>
        <div className={styles.eyebrow}><span /> WUHU 12345 SERVICE COPILOT</div>
        <h1>听懂群众诉求<br />生成可信工单。</h1>
        <p className={styles.lead}>连接诉求受理、工单生成、事项分类与部门转派，让每一步都有依据、可审核、可追溯。</p>
        <div className={styles.arch}>
          {[
            ["database", "标准工单", "要素完整与事实忠实"],
            ["brain", "辅助决策", "分类、转派与政策依据"],
            ["loop", "人工闭环", "生成 → 审核 → 确认"],
          ].map(([icon, title, text]) => <div key={title} className={styles.archItem}>
            <span className={styles.archIcon}><Icon name={icon as "database"} size={19} /></span>
            <div><b>{title}</b><small>{text}</small></div>
          </div>)}
        </div>
        <div className={styles.trust}><Icon name="shield" size={16} /> 脱敏处理 · 人工复核 · 全链路审计</div>
      </section>

      <section className={styles.loginSide}>
        <form className={styles.card} onSubmit={submit}>
          <div className={styles.mobileBrand}><span className={styles.seal}>芜</span> 芜湖政务 Agent</div>
          <div className={styles.cardHead}>
            <span className={styles.kicker}>{mode === "login" ? "WELCOME BACK" : "CREATE ACCOUNT"}</span>
            <h2>{mode === "login" ? "进入12345工作台" : "注册营业员账号"}</h2>
            <p>{mode === "login" ? "系统会依据岗位权限进入对应业务工作台" : "注册成功后将直接进入诉求受理工作台"}</p>
          </div>
          {mode === "register" && <label className={styles.field}><span>姓名</span><div className={styles.inputWrap}><Icon name="agent" size={17}/><input value={name} onChange={e => setName(e.target.value)} placeholder="请输入营业员姓名" autoFocus /></div></label>}
          <label className={styles.field}><span>账号</span><div className={styles.inputWrap}><Icon name="agent" size={17}/><input value={username} onChange={e => setUsername(e.target.value)} placeholder="3-32位字母、数字或下划线" autoFocus={mode === "login"} /></div></label>
          <label className={styles.field}><span>密码</span><div className={styles.inputWrap}><Icon name="shield" size={17}/><input type="password" value={password} onChange={e => setPassword(e.target.value)} placeholder="请输入密码" /></div></label>
          {mode === "register" && <label className={styles.field}><span>确认密码</span><div className={styles.inputWrap}><Icon name="shield" size={17}/><input type="password" value={confirmPassword} onChange={e => setConfirmPassword(e.target.value)} placeholder="请再次输入密码" /></div></label>}
          {error && <div className={styles.error}>{error}</div>}
          <button className={styles.submit} type="submit" disabled={busy}>{busy ? <><span className={styles.spinner}/>{mode === "login" ? "正在验证" : "正在注册"}</> : <>{mode === "login" ? "安全登录" : "注册并进入"} <Icon name="arrow" size={17}/></>}</button>
          <button type="button" className={styles.modeSwitch} onClick={() => { setMode(mode === "login" ? "register" : "login"); setError(""); }}>
            {mode === "login" ? "没有账号？注册营业员" : "已有账号？返回登录"}
          </button>
          {mode === "login" && <><div className={styles.divider}><span>演示身份快速进入</span></div>
          <div className={styles.demoList}>{DEMOS.map(d => <button key={d.user} type="button" className={styles.demo} onClick={() => { setUsername(d.user); setPassword(d.pass); setError(""); }}>
            <span className={styles.demoIcon}><Icon name={d.icon} size={17}/></span><span><b>{d.type}</b><small>{d.desc}</small></span><code>{d.user}</code>
          </button>)}</div></>}
          <div className={styles.security}><Icon name="shield" size={14}/> 本地演示环境 · Token 带有效期 · 操作按角色隔离</div>
        </form>
      </section>
      </main>
    </div>
  );
}
