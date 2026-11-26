import { useEffect, useState, type FormEvent } from "react";
import { describeError, jsonBody, type Api, type JobRecord, type Status } from "./api";
import { dateText, Empty, Problem, statusText, useRemote } from "./ui";

type Subject = "math" | "english";
type Point = {
  id: string; subject: Subject; title: string; unit: string; description: string;
  origin: "builtin" | "manual" | "discovered"; reviewed: boolean; assessment: "existing" | "reference";
};
type Feedback = { strengths: string[]; improvements: string[]; suggested_response: string };
type Activity = JobRecord & {
  subject: Subject; kind: "discovery" | "practice" | "feedback";
  pointId: string | null; sourceId: string | null; canRetry: boolean; eligibleForMastery: false;
};
type Discovery = { label: string; page: number; stem: string; points: Point[]; feedback: Feedback | null };
type Exercise = {
  id: string; instruction: string; context: string; answer: string | null;
  referenceAnswer: string | null; explanation: string | null; feedback: Feedback | null;
  feedbackStatus: Status | null; feedbackError: string | null; feedbackId: string | null; feedbackCanRetry: boolean;
};
type Detail = Activity & {
  point: Point | null; items: (Discovery | Exercise)[];
  retakePages: number[]; provenance: { model: string }[];
};
const pending = (activity: Activity) => activity.status === "queued" || activity.status === "running";
const pendingHistory = (activities: Activity[]) => activities.some(pending);
const pendingDetail = (detail: Detail) => pending(detail) || detail.items.some((item) =>
  "feedbackStatus" in item && (item.feedbackStatus === "queued" || item.feedbackStatus === "running"));

function FeedbackPanel({ value }: { value: Feedback }) {
  return <div className="knowledge-feedback">
    <h3>AI 参考反馈</h3>
    {value.strengths.map((text, index) => <p key={`strength-${index}`}>{text}</p>)}
    {value.improvements.map((text, index) => <p key={`improve-${index}`}>{text}</p>)}
    <strong>参考表达</strong><p>{value.suggested_response}</p>
  </div>;
}

export function KnowledgeWorkspace({ api, subject, id, focused, open, learn }: {
  api: Api; subject: Subject; id?: string; focused?: string; open: (id?: string) => void; learn: (point: Point) => void;
}) {
  const directory = useRemote<{ canManage: boolean; points: Point[] }>(api, `/knowledge-points?subject=${subject}`);
  const history = useRemote<Activity[]>(api, `/knowledge-activities?subject=${subject}`, pendingHistory);
  const [search, setSearch] = useState("");
  const [editing, setEditing] = useState<Point | null>(null);
  const [form, setForm] = useState({ title: "", unit: "", description: "" });
  const [error, setError] = useState("");
  const [saved, setSaved] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => { directory.reload(); }, [history.data, directory.reload]);

  async function save(event: FormEvent) {
    event.preventDefault();
    setError(""); setSaved(""); setBusy(true);
    try {
      await api.json(`/knowledge-points${editing ? `/${editing.id}` : ""}`,
        jsonBody(editing ? "PATCH" : "POST", { subject, ...form }));
      setSaved(editing ? "知识点已整理。" : "知识点已增加。");
      setEditing(null); setForm({ title: "", unit: "", description: "" }); setSearch("");
      directory.reload();
    } catch (reason) { setError(describeError(reason)); }
    finally { setBusy(false); }
  }

  async function practice(point: Point) {
    if (point.assessment === "existing") { learn(point); return; }
    setError(""); setBusy(true);
    try {
      const activity = await api.json<Activity>(`/knowledge-points/${point.id}/practices`,
        jsonBody("POST", { count: 4, difficulty: "foundation" }));
      open(activity.id);
    } catch (reason) { setError(describeError(reason)); }
    finally { setBusy(false); }
  }

  if (id) return <KnowledgeDetail api={api} id={id} back={() => open()} learn={practice} actionError={error} busy={busy} />;
  const points = directory.data?.points.filter((point) => (!focused || point.id === focused) &&
    `${point.title} ${point.unit} ${point.description}`.toLocaleLowerCase().includes(search.toLocaleLowerCase()));
  return <section className="knowledge-workspace">
    <div className="page-heading"><div><p className="eyebrow">共同的课程 · 私人的学习记录</p>
      <h1>共享知识点目录</h1></div>
      {directory.data?.canManage && <a className="button primary" href="#knowledge-form" onClick={() => {
        setEditing(null); setForm({ title: "", unit: "", description: "" }); setSaved("");
      }}>增加知识点</a>}
    </div>
    <p className="muted">上传作业后，系统会单独发现知识点。清晰的新知识点自动收录；题图、题目摘录和反馈只对上传者可见。</p>
    {error && <Problem>{error}</Problem>}
    {saved && <p className="notice" role="status">{saved}</p>}
    {directory.error && <Problem retry={directory.reload}>{directory.error}</Problem>}
    <div className="knowledge-layout">
      <div>
        {focused ? <button className="text-button" onClick={() => open()}>查看全部知识点</button>
          : <label className="knowledge-search" htmlFor="knowledge-search">查找知识点
            <input id="knowledge-search" value={search} onChange={(event) => setSearch(event.target.value)}
              placeholder="输入名称或章节" />
          </label>}
        {!directory.data && !directory.error && <p role="status">正在读取共享目录…</p>}
        {points?.length === 0 && <Empty title="没有匹配的知识点">换一个关键词，或由教师添加新的知识点。</Empty>}
        <div className="knowledge-grid">{points?.map((point) => <article className="panel knowledge-card" key={point.id}>
          <p className="eyebrow">{point.unit} · {point.origin === "builtin" ? "内置" : point.origin === "manual" ? "教师添加" : "作业发现"}</p>
          <h2>{point.title}</h2><p>{point.description}</p>
          <p className={point.assessment === "reference" ? "knowledge-reference" : "muted"}>
            {point.assessment === "reference" ? "AI 参考练习 · 不计入掌握度" : "使用已有的学习与校验流程"}
            {!point.reviewed && " · 待教师整理"}
          </p>
          <div className="knowledge-actions">
            <button className="button primary" disabled={busy} onClick={() => void practice(point)}>
              {point.assessment === "reference" ? "生成参考练习" : "进入练习"}
            </button>
            {directory.data?.canManage && point.origin !== "builtin" &&
              <button className="text-button" disabled={busy} onClick={() => {
                setEditing(point); setForm({ title: point.title, unit: point.unit, description: point.description });
                document.getElementById("knowledge-form")?.scrollIntoView({ block: "center" });
              }}>整理知识点</button>}
          </div>
        </article>)}</div>
      </div>
      <aside className="knowledge-sidebar">
        {directory.data?.canManage ? <section className="panel">
          <h2>{editing ? "整理知识点" : "增加知识点"}</h2>
          <p className="muted">教师 / 管理员维护。新条目对所有用户可见，审核说明不会把 AI 参考提升为可靠判分。</p>
          <form id="knowledge-form" tabIndex={-1} className="knowledge-form" onSubmit={(event) => void save(event)}>
            <label>知识点名称<input required minLength={2} maxLength={80} value={form.title}
              onChange={(event) => setForm({ ...form, title: event.target.value })} /></label>
            <label>所属章节<input required maxLength={80} value={form.unit}
              onChange={(event) => setForm({ ...form, unit: event.target.value })} /></label>
            <label>教学说明<textarea required maxLength={600} rows={4} value={form.description}
              onChange={(event) => setForm({ ...form, description: event.target.value })} /></label>
            <button className="button primary" disabled={busy}>{busy ? "正在保存…" : editing ? "保存整理" : "增加知识点"}</button>
            {editing && <button type="button" className="text-button" onClick={() => {
              setEditing(null); setForm({ title: "", unit: "", description: "" });
            }}>取消整理</button>}
          </form>
        </section> : directory.data && <div className="notice">目录由教师或管理员维护。你的作业仍可自动发现新知识点。</div>}
        <section className="panel"><h2>我的发现与参考练习</h2>
          {history.error && <Problem retry={history.reload}>{history.error}</Problem>}
          {history.data?.length === 0 && <Empty title="还没有记录">上传数学或英语作业，或选择一个知识点练习。</Empty>}
          <ul className="ledger-list">{history.data?.map((activity) => <li key={activity.id}>
            <button onClick={() => open(activity.id)}>
              <span>{activity.kind === "discovery" ? "作业知识点发现" : "AI 参考练习"} · {dateText(activity.createdAt)}</span>
              <span className={`job-state ${activity.status}`}>{statusText[activity.status]}</span>
            </button></li>)}</ul>
        </section>
      </aside>
    </div>
  </section>;
}

function KnowledgeDetail({ api, id, back, learn, actionError, busy }: {
  api: Api; id: string; back: () => void; learn: (point: Point) => void; actionError: string; busy: boolean;
}) {
  const remote = useRemote<Detail>(api, `/knowledge-activities/${id}`, pendingDetail);
  const [error, setError] = useState("");
  async function retry(activityId: string) {
    setError("");
    try { await api.json(`/knowledge-activities/${activityId}/retry`, jsonBody("POST", {})); remote.reload(); }
    catch (reason) { setError(describeError(reason)); }
  }
  const detail = remote.data;
  return <section className="knowledge-workspace">
    <button className="text-button" onClick={back}>返回共享目录</button>
    <h1>{detail?.kind === "discovery" ? "作业中的知识点" : "AI 参考练习"}</h1>
    <p className="knowledge-reference">本页提供 AI 参考，不是可靠判分，所有回答均不计入掌握度。</p>
    {detail?.provenance.some((version) => version.model === "synthetic-demo") &&
      <p className="notice">这是本地合成示例，尚未调用真实 AI。</p>}
    {remote.error && <Problem retry={remote.reload}>{remote.error}</Problem>}
    {error && <Problem>{error}</Problem>}
    {actionError && <Problem>{actionError}</Problem>}
    {detail && pending(detail) && <p role="status">正在{detail.kind === "discovery" ? "发现知识点" : "生成参考练习"}，可以稍后回来查看。</p>}
    {detail?.status === "failed" && <Problem>{detail.error ?? "任务未完成，请重新创建。"}</Problem>}
    {detail?.canRetry && <button className="button" onClick={() => void retry(id)}>重试任务</button>}
    {detail?.kind === "discovery" && detail.sourceId && <p><a href={detail.subject === "math"
      ? `?view=report&id=${detail.sourceId}` : `?view=english&id=${detail.sourceId}`}>查看原作业报告</a></p>}
    {detail?.status === "done" && detail.items.length === 0 &&
      <Empty title="未发现明确的知识点">请检查图片是否清晰、完整；这不代表作业已被正确判分。</Empty>}
    {detail?.retakePages.length ? <p className="notice">第 {detail.retakePages.join("、")} 页不够清晰，未自动新增知识点。</p> : null}
    <div className="knowledge-results">{detail?.items.map((item, index) => "stem" in item
      ? <article className="panel" key={`${item.page}:${index}`}>
        <p className="eyebrow">第 {item.page} 页 · 第 {item.label} 题</p><h2>{item.stem}</h2>
        <div className="knowledge-actions">{item.points.map((point) =>
          <button key={point.id} className="button" disabled={busy} onClick={() => learn(point)}>{point.title} · 去练习</button>)}</div>
        {item.feedback && <FeedbackPanel value={item.feedback} />}
      </article>
      : <ReferenceCard key={item.id} api={api} activityId={id} item={item} number={index + 1}
        reload={remote.reload} retry={retry} />)}</div>
  </section>;
}

function ReferenceCard({ api, activityId, item, number, reload, retry }: {
  api: Api; activityId: string; item: Exercise; number: number; reload: () => void; retry: (id: string) => Promise<void>;
}) {
  const [answer, setAnswer] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  async function submit(event: FormEvent) {
    event.preventDefault(); setError(""); setBusy(true);
    try {
      await api.json(`/knowledge-activities/${activityId}/items/${item.id}/answer`,
        jsonBody("POST", { answer }));
      reload();
    } catch (reason) { setError(describeError(reason)); }
    finally { setBusy(false); }
  }
  return <article className="panel">
    <p className="eyebrow">参考练习 {number}</p><h2>{item.instruction}</h2>
    {item.context && <p className="knowledge-context">{item.context}</p>}
    {error && <Problem>{error}</Problem>}
    {!item.answer ? <form className="knowledge-form" onSubmit={(event) => void submit(event)}>
      <label>第 {number} 题参考作答<textarea required maxLength={4000} rows={3}
        value={answer} onChange={(event) => setAnswer(event.target.value)} /></label>
      <button className="button primary" disabled={busy}>{busy ? "正在提交…" : "提交并获取参考反馈"}</button>
    </form> : <><strong>你的回答</strong><p>{item.answer}</p>
      <details><summary>查看未经校验的 AI 参考答案</summary>
        <p>{item.referenceAnswer}</p><p>{item.explanation}</p></details></>}
    {(item.feedbackStatus === "queued" || item.feedbackStatus === "running") &&
      <p role="status">正在生成参考反馈…</p>}
    {item.feedbackError && <Problem>{item.feedbackError}</Problem>}
    {item.feedbackCanRetry && item.feedbackId &&
      <button className="button" onClick={() => { if (item.feedbackId) void retry(item.feedbackId); }}>重试参考反馈</button>}
    {item.feedback && <FeedbackPanel value={item.feedback} />}
  </article>;
}
