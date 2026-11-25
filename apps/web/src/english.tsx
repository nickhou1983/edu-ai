import { useEffect, useState } from "react";
import {
  describeError, jsonBody, masteryNames, pending, type Api, type JobRecord, type Mastery, type Status,
} from "./api";
import { dateText, Empty, Problem, statusText, useRemote } from "./ui";

type EnglishSkill = Omit<Mastery, "status"> & { status: Mastery["status"] | "feedback_only"; hint: string };
type Feedback = { strengths: string[]; improvements: string[]; suggested_response: string };
type Prompt = { skill: string; mode: "choice" | "fill" | "open"; instruction: string; context: string; choices: string[] };
type EnglishItem = {
  id: string; page: number | null; label: string | null; prompt: Prompt | null;
  answer: string | null; outcome: "correct" | "incorrect" | "blank" | "undetermined" | "feedback" | null;
  counted: boolean; feedback: Feedback | null; expected: string[] | null; explanation: string | null;
  feedbackId?: string; feedbackStatus: Status | null; feedbackError: string | null; feedbackCanRetry?: boolean;
};
type EnglishRecord = JobRecord & {
  kind: "worksheet" | "practice" | "feedback"; skill: string | null; canRetry: boolean;
};
type EnglishDetail = EnglishRecord & {
  pages: number[]; items: EnglishItem[]; retakePages: number[];
  provenance: { model: string; prompt: string; agent: string }[];
};
const skillNames: Record<string, string> = {
  "english-vocabulary": "词汇与词形", "english-grammar": "基础语法", "english-reading": "阅读理解",
  "english-writing": "写作", "english-listening-text": "听力文本预习", "english-speaking-text": "口语表达草稿",
};
const skillEnglish: Record<string, string> = {
  "english-vocabulary": "Vocabulary", "english-grammar": "Grammar", "english-reading": "Reading",
  "english-writing": "Writing", "english-listening-text": "Listening · text", "english-speaking-text": "Speaking · text",
};
const labels = {
  correct: "正确", incorrect: "错误", blank: "未作答", undetermined: "无法判定", feedback: "文字反馈",
};
const pollHistory = (records: EnglishRecord[]) => records.some(pending);
const pollDetail = (record: EnglishDetail) => pending(record)
  || record.items.some((item) => item.feedbackStatus === "queued" || item.feedbackStatus === "running");

export function EnglishWorkspace({ api, sample, sampleOnly, id, focused, open, focus }: {
  api: Api; sample: boolean; sampleOnly: boolean; id?: string; focused?: string;
  open: (id?: string) => void; focus: (skill: string) => void;
}) {
  const skills = useRemote<EnglishSkill[]>(api, "/english/skills");
  const history = useRemote<EnglishRecord[]>(api, "/english/records", pollHistory);
  const [files, setFiles] = useState<File[]>([]);
  const [difficulty, setDifficulty] = useState("foundation");
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const recommended = [...(skills.data ?? [])].filter((s) => s.status !== "feedback_only").sort((a, b) => {
    const rank = { priority: 0, insufficient: 1, reinforce: 2, stable: 3, feedback_only: 4 };
    return rank[a.status] - rank[b.status] || (a.percent ?? 0) - (b.percent ?? 0);
  })[0];
  const selected = focused && skillNames[focused] ? focused : recommended?.id ?? "english-vocabulary";

  async function upload(useSample = false) {
    setError("");
    setBusy("upload");
    try {
      const body = new FormData();
      if (useSample) body.append("files", await api.blob("/english/sample"), "english-synthetic.png");
      else files.forEach((file) => body.append("files", file));
      const record = await api.json<EnglishRecord>("/english/worksheets", { method: "POST", body });
      open(record.id);
    } catch (reason) { setError(describeError(reason)); }
    finally { setBusy(""); }
  }

  async function generate() {
    setError("");
    setBusy("generate");
    try {
      const record = await api.json<EnglishRecord>("/english/practices", jsonBody("POST", {
        skill: selected, difficulty, count: 4,
      }));
      open(record.id);
    } catch (reason) { setError(describeError(reason)); }
    finally { setBusy(""); }
  }

  if (id) return <EnglishRecordView key={id} api={api} id={id} back={() => {
    skills.reload(); history.reload(); open();
  }} />;

  return <div className="english-workspace">
    <section className="english-heading" aria-labelledby="english-title">
      <div><p className="eyebrow section-code">ENGLISH / 初一通用 · 不绑定教材</p>
        <h1 id="english-title"><span>把一句话，</span><br /><span>读懂、写清。</span></h1></div>
      <div className="english-notebook">
        <p lang="en">Read it. Try it.<br /><em>Make it yours.</em></p>
        <span>词汇与语法 → 理解 → 表达</span>
      </div>
    </section>
    <p className="callout">听力与口语目前只提供文本预习和表达草稿；不播放、录制音频，也不评估发音或真实听说能力。</p>
    {sampleOnly && <p className="muted small">本地合成演示：只分析自带英语样卷；生成内容和文字反馈均为固定样例，不代表真实模型效果。</p>}
    {error && <Problem>{error}</Problem>}
    <div className="english-top">
      <section className="panel" aria-labelledby="english-upload">
        <p className="eyebrow section-code">从已有作业开始</p>
        <h2 id="english-upload">英语作业与试卷</h2>
        <p className="muted">上传 1–4 页截图，检查词汇、语法、阅读作答，查看写作的文字建议。每张不超过 8 MB。</p>
        <input id="english-files" type="file" accept="image/png,image/jpeg,image/webp" multiple
          aria-label="选择英语作业图片" onChange={(event) => {
            setFiles(Array.from(event.target.files ?? [])); setError("");
          }} />
        {files.length > 0 && <ul className="file-list">{files.map((file, i) => <li key={`${file.name}:${i}`}>
          <span className="file-name">{file.name}</span>
        </li>)}</ul>}
        <div className="actions">
          <button className="button primary" disabled={!!busy || files.length < 1 || files.length > 4}
            onClick={() => void upload()}>{busy === "upload" ? "正在上传…" : "分析英语作业"}</button>
          {sample && <button className="text-button" disabled={!!busy} onClick={() => void upload(true)}>试用英语合成样卷</button>}
        </div>
        {files.length > 4 && <Problem>一次最多选择 4 页，请重新选择图片。</Problem>}
      </section>
      <section className="panel english-practice-form" aria-labelledby="english-generate">
        <p className="eyebrow section-code">从知识点继续练</p>
        <h2 id="english-generate">下一组英语练习</h2>
        <p className="muted small">优先推荐薄弱项；证据不足时先做诊断。写作和听说文本反馈单独展示。</p>
        <div className="inline-form">
          <label htmlFor="english-skill">英语练习方向</label>
          <select id="english-skill" value={selected} onChange={(event) => focus(event.target.value)}>
            {Object.entries(skillNames).map(([skill, name]) => <option key={skill} value={skill}>{name}</option>)}
          </select>
          <label htmlFor="english-difficulty">英语练习难度</label>
          <select id="english-difficulty" value={difficulty} onChange={(event) => setDifficulty(event.target.value)}>
            <option value="foundation">基础</option><option value="standard">进阶</option>
          </select>
          <button className="button primary" disabled={!!busy} onClick={() => void generate()}>
            {busy === "generate" ? "正在创建…" : "生成 4 道英语练习"}</button>
        </div>
      </section>
    </div>
    <section id="english-skills" className="unit-section">
      <div className="section-heading"><h2>英语能力地图</h2><span className="muted small">客观证据与表达建议，分开记录</span></div>
      <p className="muted small">只汇总词汇、语法、阅读的最近 10 道不同题的有效作答；少于 3 道不显示百分比。AI 参考答案仍需核对。</p>
      {skills.error && <Problem retry={skills.reload}>{skills.error}</Problem>}
      {!skills.data && !skills.error && <p role="status">正在读取英语学习记录…</p>}
      <div className="english-skill-grid">{skills.data?.map((skill) => <article className="panel" key={skill.id}>
        <p className="english-caption" lang="en">{skillEnglish[skill.id]}</p>
        <h3>{skill.title}</h3>
        <div className="skill-metric">
          {skill.status === "feedback_only" ? <span>仅展示文字建议，不计入客观掌握度</span>
            : <><b>{skill.percent === null ? "—" : `${skill.percent}%`}</b>
              <span className={`pill ${skill.status}`}>{masteryNames[skill.status]}</span></>}
        </div>
        {skill.status !== "feedback_only" && <p className="muted small">有效作答 {skill.sampleCount} 道</p>}
        <p className="muted small">{skill.hint}</p>
        <button className="text-button" onClick={() => {
          focus(skill.id);
          document.getElementById("english-generate")?.scrollIntoView({ block: "center" });
        }}>练习{skill.title} →</button>
        {skill.evidence.length > 0 && <details><summary>查看英语作答依据</summary>
          <ul className="evidence-list">{skill.evidence.map((entry) => <li key={entry.itemId}>
            <span>{dateText(entry.occurredAt)} · {entry.score === 2 ? "正确" : "错误"}</span>
            <button className="text-button" onClick={() => { if (entry.englishId) open(entry.englishId); }}>查看原记录</button>
          </li>)}</ul>
        </details>}
      </article>)}</div>
    </section>
    <section id="english-history" className="panel unit-section">
      <h2>英语学习记录</h2>
      {history.error && <Problem retry={history.reload}>{history.error}</Problem>}
      {history.data?.length === 0 && <Empty title="还没有英语学习记录">上传合成样卷，或选择一个方向生成练习。</Empty>}
      <ul className="ledger-list">{history.data?.map((record) => <li key={record.id}>
        <button onClick={() => open(record.id)}>
          <span>{record.kind === "worksheet" ? "英语作业报告" : skillNames[record.skill ?? ""] ?? "英语练习"}</span>
          <span className="muted small">{dateText(record.createdAt)}</span>
          <span className={`job-state ${record.status}`}>{statusText[record.status]}</span>
        </button>
      </li>)}</ul>
    </section>
  </div>;
}

function EnglishRecordView({ api, id, back }: { api: Api; id: string; back: () => void }) {
  const remote = useRemote<EnglishDetail>(api, `/english/records/${id}`, pollDetail);
  const [error, setError] = useState("");
  const [retrying, setRetrying] = useState(false);
  async function retry(recordId = id) {
    setError(""); setRetrying(true);
    try { await api.json(`/english/records/${recordId}/retry`, { method: "POST" }); remote.reload(); }
    catch (reason) { setError(describeError(reason)); }
    finally { setRetrying(false); }
  }
  const record = remote.data;
  return <div className="english-workspace">
    <div className="page-title">
      <button className="text-button back" onClick={back}>← 返回英语学习</button>
      <p className="eyebrow section-code">ENGLISH / 初一通用</p>
      <h1>{record?.kind === "worksheet" ? "英语作业报告" : "英语练习"}</h1>
    </div>
    <p className="callout">英语答案是 AI 学习参考，并非教师标准答案；有争议时请核对原题。写作和听说文本没有客观分数。</p>
    {remote.error && <Problem retry={remote.reload}>{remote.error}</Problem>}
    {error && <Problem>{error}</Problem>}
    {!record && !remote.error && <p role="status">正在读取英语记录…</p>}
    {record && pending(record) && <div className="notice progress" role="status">
      {record.status === "queued" ? "英语任务正在排队" : "正在处理英语学习内容"}；可以稍后从记录中继续查看。
    </div>}
    {record?.status === "failed" && <>
      <Problem>{record.error ?? "英语任务未完成。"}</Problem>
      {record.canRetry && <button className="button secondary" disabled={retrying} onClick={() => void retry()}>重新处理英语任务</button>}
    </>}
    {!!record?.retakePages.length && <p className="notice advisory">第 {record.retakePages.join("、")} 页未能清晰识别；请裁掉无关内容，重新拍摄上传，未据此计入掌握度。</p>}
    {record && <div className={record.kind === "worksheet" ? "english-report-grid" : ""}>
      {record.pages.length > 0 && <section className="panel"><h2>核对原图</h2>
        {record.pages.map((number) => <EnglishPage key={number} api={api} id={id} number={number} />)}
      </section>}
      <section className="english-items" aria-label="英语题目">
        {record.status === "done" && record.items.length === 0 && <Empty title="没有可展示的题目">请检查图片是否完整、清晰。</Empty>}
        {record.items.map((item, index) => <EnglishItemCard key={item.id} api={api} record={record} item={item}
          index={index} reload={remote.reload} retry={(feedbackId) => void retry(feedbackId)} retrying={retrying} />)}
      </section>
    </div>}
    {record?.provenance.some((version) => version.model === "synthetic-demo") &&
      <p className="muted small end-link">本地合成样例：结果为演示数据，不代表真实 AI 分析。</p>}
  </div>;
}

function EnglishPage({ api, id, number }: { api: Api; id: string; number: number }) {
  const [source, setSource] = useState("");
  const [error, setError] = useState("");
  const [generation, setGeneration] = useState(0);
  useEffect(() => {
    const abort = new AbortController();
    let url = "";
    setError("");
    void api.blob(`/english/records/${id}/pages/${number}`, abort.signal).then((blob) => {
      if (abort.signal.aborted) return;
      url = URL.createObjectURL(blob); setSource(url);
    }).catch((reason) => { if (!abort.signal.aborted) setError(describeError(reason)); });
    return () => { abort.abort(); if (url) URL.revokeObjectURL(url); };
  }, [api, id, number, generation]);
  return <figure className="english-original">
    <figcaption className="muted small">第 {number} 页</figcaption>
    {error && <Problem retry={() => setGeneration((g) => g + 1)}>{error}</Problem>}
    {source && <img src={source} alt={`英语作业第 ${number} 页原图`} />}
  </figure>;
}

function EnglishItemCard({ api, record, item, index, reload, retry, retrying }: {
  api: Api; record: EnglishDetail; item: EnglishItem; index: number; reload: () => void;
  retry: (id: string) => void; retrying: boolean;
}) {
  const [answer, setAnswer] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const prompt = item.prompt;
  const unsubmitted = record.kind === "practice" && item.answer === null;
  async function submit(event: React.FormEvent) {
    event.preventDefault(); setBusy(true); setError("");
    try {
      await api.json(`/english/records/${record.id}/items/${item.id}/answer`, jsonBody("POST", { answer }));
      reload();
    } catch (reason) { setError(describeError(reason)); }
    finally { setBusy(false); }
  }
  return <article className="panel english-question">
    <div className="section-heading">
      <p className="eyebrow section-code">英语第 {item.label ?? index + 1} 题{item.page ? ` · 第 ${item.page} 页` : ""}</p>
      {item.outcome && <span className={`pill ${item.outcome}`}>{labels[item.outcome]}</span>}
    </div>
    <h2>{prompt?.instruction ?? "题目无法可靠转写"}</h2>
    {prompt?.context && <blockquote className="english-passage" lang="en">{prompt.context}</blockquote>}
    {prompt && prompt.choices.length > 0 && !unsubmitted && <ol className="english-choices" type="A">
      {prompt.choices.map((choice, i) => <li key={i} lang="en">{choice}</li>)}</ol>}
    {unsubmitted && prompt && <form className="inline-form" onSubmit={(event) => void submit(event)}>
      {prompt.mode === "choice" ? <fieldset className="options">
        <legend>英语第 {index + 1} 题选项</legend>
        {prompt.choices.map((choice, i) => <label key={i}>
          <input type="radio" name={`english-${item.id}`} value={"ABCD"[i]} checked={answer === "ABCD"[i]}
            onChange={(event) => setAnswer(event.target.value)} disabled={busy} /> {"ABCD"[i]}. <span lang="en">{choice}</span>
        </label>)}
      </fieldset> : <>
        <label htmlFor={`answer-${item.id}`}>英语第 {index + 1} 题作答</label>
        {prompt.mode === "open" ? <textarea id={`answer-${item.id}`} lang="en" rows={5} maxLength={4000}
          value={answer} onChange={(event) => setAnswer(event.target.value)} required disabled={busy} />
          : <input id={`answer-${item.id}`} lang="en" value={answer} maxLength={256} autoComplete="off"
            onChange={(event) => setAnswer(event.target.value)} required disabled={busy} />}
      </>}
      <p className="muted small">{prompt.mode === "open"
        ? "提交后获取 AI 文字建议，不计分、不计入客观掌握度。"
        : "先独立作答，再查看参考。保留必要的大小写和标点。"}</p>
      <button className="button primary" disabled={busy || !answer.trim()}>{busy ? "正在提交…" : "提交英语作答"}</button>
    </form>}
    {error && <Problem>{error}</Problem>}
    {item.answer !== null && <div className="english-answer"><span className="muted small">你的作答</span><p lang="en">{item.answer}</p></div>}
    {!!item.expected?.length && <p className="english-reference">AI 参考：<strong lang="en">{item.expected.join(" / ")}</strong></p>}
    {item.explanation && item.outcome !== "feedback" && <p className="muted small">{item.explanation}</p>}
    {item.outcome === "undetermined" && <p className="muted">识别或参考答案不足以可靠判定，本题不计入掌握度。</p>}
    {item.counted && <p className="muted small">作为客观作答证据记录；同题只计第一次有效作答。</p>}
    {item.feedbackStatus && (item.feedbackStatus === "queued" || item.feedbackStatus === "running") &&
      <p className="notice progress" role="status">正在生成文字建议，不计入客观掌握度…</p>}
    {item.feedbackStatus === "failed" && <>
      <Problem>{item.feedbackError ?? "暂时未能生成文字建议。"}</Problem>
      {item.feedbackCanRetry && item.feedbackId && <button className="button secondary" disabled={retrying}
        onClick={() => retry(item.feedbackId!)}>重新获取文字建议</button>}
    </>}
    {item.feedback && <div className="english-feedback">
      <h3>AI 文字建议 · 不计分</h3>
      <p className="small">已经做到</p><ul>{item.feedback.strengths.map((text, i) => <li key={i}>{text}</li>)}</ul>
      <p className="small">下一步改进</p><ul>{item.feedback.improvements.map((text, i) => <li key={i}>{text}</li>)}</ul>
      <p className="small">一种表达方式，不是唯一答案</p><blockquote lang="en">{item.feedback.suggested_response}</blockquote>
    </div>}
  </article>;
}
