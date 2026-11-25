import type { AccountInfo, PublicClientApplication } from "@azure/msal-browser";
import { useEffect, useMemo, useState } from "react";
import { createRoot } from "react-dom/client";
import { apiClient, type Api, type Evidence, type SessionConfig } from "./api";
import { Desk } from "./desk";
import { EnglishWorkspace } from "./english";
import { MasteryView, PracticeDesk, PracticeView } from "./practice";
import { ReportView } from "./report";
import { Icon, Problem } from "./ui";
import "./styles.css";

type View = "desk" | "report" | "mastery" | "practice" | "exercise" | "english";
type Route = { view: View; id?: string; item?: string; skill?: string };
type ScreenProps = { api: Api; badge: string; sample?: boolean; sampleOnly?: boolean; identity: React.ReactNode };
const publicApi = apiClient();

function routeFromLocation(): Route {
  const params = new URLSearchParams(location.search);
  const view = params.get("view");
  const id = params.get("id");
  const item = params.get("item") ?? undefined;
  if (view === "english") return { view, id: id ?? undefined, skill: params.get("skill") ?? undefined };
  if ((view === "report" || view === "exercise") && id) return { view, id, item };
  if (view === "mastery") return { view };
  if (view === "practice") return { view, skill: params.get("skill") ?? undefined };
  return { view: "desk" };
}

function Workspace({ api, badge, sample = false, sampleOnly = false, identity }: ScreenProps) {
  const [route, setRoute] = useState(routeFromLocation);
  useEffect(() => {
    const update = () => setRoute(routeFromLocation());
    window.addEventListener("popstate", update);
    return () => window.removeEventListener("popstate", update);
  }, []);

  function go(next: Route) {
    const params = new URLSearchParams();
    if (next.view !== "desk") params.set("view", next.view);
    if (next.id) params.set("id", next.id);
    if (next.item) params.set("item", next.item);
    if (next.skill) params.set("skill", next.skill);
    window.history.pushState(null, "", params.size ? `?${params}` : location.pathname);
    setRoute(next);
    window.scrollTo({ top: 0, behavior: "instant" });
  }

  const viewMastery = () => go({ view: "mastery" });
  const viewDesk = () => go({ view: "desk" });
  const viewPractice = (skill?: string) => go({ view: "practice", skill });
  const viewReport = (id: string) => go({ view: "report", id });
  const viewExercise = (id: string) => go({ view: "exercise", id });
  const nav = route.view === "report" ? "desk" : route.view === "exercise" ? "practice" : route.view;

  function viewEvidence(entry: Evidence) {
    if (entry.submissionId) go({ view: "report", id: entry.submissionId, item: entry.itemId });
    if (entry.practiceId) go({ view: "exercise", id: entry.practiceId, item: entry.itemId });
  }

  return <>
    <a className="skip-link" href="#content">跳到内容</a>
    <header className="site-header">
      <div className="header-wrap">
        <button onClick={viewDesk} className="brand"><span className="brand-icon"><Icon name="mark" size={24} /></span>作业回路</button>
        {route.view !== "english" ? <nav aria-label="主导航">
          <button aria-current={nav === "desk" ? "page" : undefined} onClick={viewDesk}>作业台</button>
          <button aria-current={nav === "mastery" ? "page" : undefined} onClick={viewMastery}>知识点地图</button>
          <button aria-current={nav === "practice" ? "page" : undefined} onClick={() => viewPractice()}>练习台</button>
        </nav> : <nav aria-label="英语导航">
          <button aria-current={!route.id ? "page" : undefined} onClick={() => go({ view: "english" })}>英语学习</button>
          <button onClick={() => { go({ view: "english" }); setTimeout(() => document.getElementById("english-history")?.scrollIntoView(), 0); }}>学习记录</button>
          <button onClick={() => { go({ view: "english" }); setTimeout(() => document.getElementById("english-skills")?.scrollIntoView(), 0); }}>能力地图</button>
        </nav>}
        <div className="identity"><span className="environment">{badge}</span>{identity}</div>
      </div>
    </header>
    <main className="wrap" id="content">
      <div className="subject-switch" role="group" aria-label="学习科目">
        <button aria-pressed={route.view !== "english"} onClick={viewDesk}>数学</button>
        <button aria-pressed={route.view === "english"} onClick={() => go({ view: "english" })}>英语</button>
        <span>初一 · 学习回路</span>
      </div>
      {route.view === "english" && <EnglishWorkspace api={api} sample={sample} sampleOnly={sampleOnly}
        id={route.id} focused={route.skill} open={(id) => go({ view: "english", id, skill: route.skill })}
        focus={(skill) => go({ view: "english", skill })} />}
      {route.view === "desk" && <Desk api={api} sample={sample} sampleOnly={sampleOnly} open={viewReport} mastery={viewMastery} />}
      {route.view === "report" && route.id && <ReportView key={`${route.id}:${route.item}`} api={api} id={route.id}
        initialItem={route.item ?? null} back={viewDesk} practice={viewPractice} />}
      {route.view === "mastery" && <MasteryView api={api} practice={viewPractice} evidence={viewEvidence} />}
      {route.view === "practice" && <PracticeDesk key={route.skill} api={api} focused={route.skill ?? null} open={viewExercise} />}
      {route.view === "exercise" && route.id && <PracticeView key={route.id} api={api} id={route.id}
        focus={route.item ?? null} back={() => viewPractice()} mastery={viewMastery} />}
      <footer><span>作业回路 / 初一{route.view === "english" ? "英语" : "数学"}</span><span>辅助学习，不用于正式评分。</span></footer>
    </main>
  </>;
}

function LocalSession({ config }: { config: Extract<SessionConfig, { mode: "local" }> }) {
  const [student, setStudent] = useState(() => {
    const saved = sessionStorage.getItem("edu-demo-student");
    return saved && config.demoStudents.includes(saved) ? saved : config.demoStudents[0];
  });
  const api = useMemo(() => apiClient(async () => ({ "X-Demo-Student": student })), [student]);
  return <Workspace key={student} api={api} badge={config.sampleOnly ? "本地演示" : "本地联调"}
    sample sampleOnly={config.sampleOnly}
    identity={<select aria-label="演示身份" value={student} onChange={(event) => {
      history.replaceState(null, "", location.pathname);
      sessionStorage.setItem("edu-demo-student", event.target.value);
      setStudent(event.target.value);
    }}>{config.demoStudents.map((owner, index) => <option key={owner} value={owner}>学生 {index + 1}</option>)}</select>} />;
}

function EntraSession({ config }: { config: Extract<SessionConfig, { mode: "entra" }> }) {
  const [client, setClient] = useState<PublicClientApplication | null>(null);
  const [account, setAccount] = useState<AccountInfo | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  useEffect(() => {
    let active = true;
    import("@azure/msal-browser").then(async ({ PublicClientApplication }) => {
      const msal = new PublicClientApplication({ auth: {
        clientId: config.clientId, authority: `https://login.microsoftonline.com/${config.tenantId}`,
        redirectUri: `${location.origin}/auth-redirect.html`, postLogoutRedirectUri: location.origin,
      }, cache: { cacheLocation: "sessionStorage" } });
      await msal.initialize();
      if (active) {
        setClient(msal);
        setAccount(msal.getActiveAccount() ?? msal.getAllAccounts()[0] ?? null);
      }
    }).catch(() => {
      if (active) setError("登录组件未能启动，请刷新页面后再试。");
    }).finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [config]);

  const api = useMemo(() => apiClient(async () => {
    if (!client || !account) throw new Error("请先登录。");
    try {
      const token = await client.acquireTokenSilent({ account, scopes: [config.scope] });
      return { Authorization: `Bearer ${token.accessToken}` };
    } catch {
      throw new Error("登录已失效，请退出后重新登录。");
    }
  }), [account, client, config.scope]);
  if (client && account) return <Workspace key={account.homeAccountId} api={api} badge="Azure 测试"
    identity={<button className="text-button" onClick={() => {
      void client.logoutRedirect({ account }).catch(() => { setAccount(null); setError("未能完成退出，请刷新。"); });
    }}>退出</button>} />;
  return <main className="wrap login">
    <div className="panel"><p className="eyebrow section-code">作业回路 · 受控测试</p><h1>登录作业回路</h1>
      <p className="muted">使用获准的 Microsoft 账号进入。测试环境只上传合成样本。</p>
      {error && <Problem>{error}</Problem>}
      <button className="button primary" disabled={loading || !client} onClick={() => {
        if (!client) return;
        setLoading(true);
        setError("");
        void client.loginPopup({ scopes: [config.scope] }).then((result) => {
          client.setActiveAccount(result.account);
          setAccount(result.account);
        }).catch(() => setError("无法登录。确认账号已获准，并允许浏览器打开登录窗口。"))
          .finally(() => setLoading(false));
      }}>{loading ? "正在连接…" : "使用 Microsoft 登录"}</button>
    </div>
  </main>;
}

function App() {
  const [config, setConfig] = useState<SessionConfig | null>(null);
  const [error, setError] = useState("");
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    const abort = new AbortController();
    setError("");
    publicApi.json<SessionConfig>("/session", { signal: abort.signal }).then(setConfig).catch((reason: unknown) => {
      if (!abort.signal.aborted) setError(reason instanceof Error ? reason.message : "无法连接服务。");
    });
    return () => abort.abort();
  }, [retry]);
  if (!config) return <main className="wrap login">{error
    ? <Problem retry={() => setRetry((value) => value + 1)}>{error}</Problem>
    : <p role="status">正在打开练习簿…</p>}</main>;
  return config.mode === "local" ? <LocalSession config={config} /> : <EntraSession config={config} />;
}

createRoot(document.getElementById("root")!).render(<App />);
