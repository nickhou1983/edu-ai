import { useEffect, useState } from "react";
import type { FormEvent } from "react";
import {
  describeError, jsonBody, masteryNames, pending, type AnswerResult, type Api, type Difficulty,
  type Evidence, type Mastery, type PracticeDetail, type PracticeItem, type PracticeSet,
} from "./api";
import { dateText, Empty, Icon, Problem, Progress, statusText, useRemote } from "./ui";

const historyPending = (records: PracticeSet[]) => records.some(pending);

export function MasteryView({ api, practice, reference, evidence }: {
  api: Api; practice: (skill: string) => void; reference: (point: string) => void;
  evidence: (item: Evidence) => void;
}) {
  const stats = useRemote<Mastery[]>(api, "/mastery");
  const units = [...new Set(stats.data?.map((item) => item.unit) ?? [])];
  return <>
    <div className="page-title">
      <p className="eyebrow section-code">地图 · 从作答找线索</p>
      <h1>知识点地图</h1>
      <p className="muted">掌握度估计参考最近 10 道不同题的有效作答。少于 3 道显示证据不足。AI 复核结果仅作学习参考。</p>
    </div>
    {stats.error && <Problem retry={stats.reload}>{stats.error}</Problem>}
    {!stats.data && !stats.error && <p role="status">正在读取掌握情况…</p>}
    {units.map((unit) => <section className="unit-section" key={unit}>
      <h2>{unit}</h2>
      <div className="skill-grid">
        {stats.data?.filter((item) => item.unit === unit).map((item) => {
          const ai = item.evidence.filter((entry) => entry.verification === "ai_review").length;
          const rules = item.sampleCount - ai;
          return <article className="panel skill-card" key={item.id}>
            <div className="section-heading"><h3>{item.title}</h3><span className={`pill ${item.status}`}>{masteryNames[item.status]}</span></div>
            <div className="skill-metric"><b>{item.percent === null ? "—" : `${item.percent}%`}</b><span>有效作答 {item.sampleCount} 道</span></div>
            {item.percent !== null ? <progress max={100} value={item.percent} aria-label={`${item.title}的得分率`} /> :
              <div className="empty-meter" aria-hidden="true" />}
            {item.sampleCount > 0 && <div className="skill-sources">
              {rules > 0 && <span className="pill source-rules">{rules} 道规则核验</span>}
              {ai > 0 && <span className="pill source-ai">{ai} 道 AI 复核</span>}
              {ai > 0 && rules === 0 && <span className="small muted">仅 AI 参考</span>}
            </div>}
            <div className="actions">
              {item.practiceMode === "reference"
                ? <button className="text-button" onClick={() => reference(item.id)}>查看参考练习</button>
                : <button className="text-button" onClick={() => practice(item.id)}>
                  {item.status === "insufficient" ? "做诊断练习" : "练这个知识点"} <Icon name="arrow" size={16} />
                </button>}
            </div>
            {item.evidence.length > 0 && <details>
              <summary>查看作答依据</summary>
              <ul className="evidence-list">{item.evidence.map((entry) => <li key={entry.itemId}>
                <button className="text-button" onClick={() => evidence(entry)}>
                  {entry.submissionId ? "作业题目" : "练习题目"} · {dateText(entry.occurredAt)}
                  {entry.verification === "ai_review" ? " · AI 复核" : " · 规则核验"}
                </button>
                <span className="small muted">{["错误", "部分正确", "正确"][entry.score]}</span>
              </li>)}</ul>
            </details>}
          </article>;
        })}
      </div>
    </section>)}
  </>;
}

export function PracticeDesk({ api, focused, open }: { api: Api; focused: string | null; open: (id: string) => void }) {
  const skills = useRemote<Mastery[]>(api, "/mastery?rules_only=true");
  const history = useRemote<PracticeSet[]>(api, "/practice-sets", historyPending);
  const [choice, setChoice] = useState(focused ?? "");
  const [difficulty, setDifficulty] = useState<Difficulty>("foundation");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const selected = skills.data?.find((skill) => skill.id === choice) ??
    skills.data?.find((skill) => skill.status === "priority") ?? skills.data?.[0];

  async function start(event: FormEvent) {
    event.preventDefault();
    if (!selected) return;
    setError("");
    setBusy(true);
    try {
      const record = await api.json<PracticeSet>("/practice-sets",
        jsonBody("POST", { skill: selected.id, count: 4, difficulty }));
      open(record.id);
    } catch (reason) {
      setError(describeError(reason));
    } finally {
      setBusy(false);
    }
  }

  return <>
    <div className="page-title">
      <p className="eyebrow section-code">巩固 · 一次一个知识点</p>
      <h1>针对性练习</h1>
      <p className="muted">先独立做，再核对答案。系统会用可核验的首次作答更新掌握度。</p>
    </div>
    <div className="desk-grid">
      <section className="panel">
        <h2>选一个知识点</h2>
        {skills.error && <Problem retry={skills.reload}>{skills.error}</Problem>}
        <form className="inline-form" onSubmit={(event) => void start(event)}>
          <label htmlFor="practice-skill">这次想练</label>
          <select id="practice-skill" value={selected?.id ?? ""} disabled={!selected || busy}
            onChange={(event) => setChoice(event.target.value)}>
            {skills.data?.map((skill) => <option key={skill.id} value={skill.id}>{skill.title}</option>)}
          </select>
          <label htmlFor="difficulty">难度</label>
          <select id="difficulty" value={difficulty} disabled={busy}
            onChange={(event) => setDifficulty(event.target.value === "standard" ? "standard" : "foundation")}>
            <option value="foundation">基础</option><option value="standard">进阶</option>
          </select>
          {selected && <p className="small callout">{selected.status === "insufficient" ? "诊断练习" : "巩固练习"} · 4 道 · 发布前逐题核验</p>}
          {error && <Problem>{error}</Problem>}
          <button className="button primary" disabled={!selected || busy}>
            {busy ? "正在提交…" : "生成 4 道练习"} <Icon name="arrow" size={16} />
          </button>
        </form>
      </section>
      <aside className="panel ledger">
        <h2>最近练习</h2>
        {history.error && <Problem retry={history.reload}>{history.error}</Problem>}
        {history.data?.length === 0 && <Empty title="还没有练习记录">生成一组，开始巩固一个知识点。</Empty>}
        {history.data && history.data.length > 0 && <ul className="ledger-list">{history.data.map((entry) => <li key={entry.id}>
          <button onClick={() => open(entry.id)}><Icon name="layers" />
            <span>{skills.data?.find((skill) => skill.id === entry.skill)?.title ?? "数学练习"} · {dateText(entry.createdAt)}</span>
            <span className={`job-state ${entry.status}`}>{statusText[entry.status]}</span>
          </button></li>)}</ul>}
      </aside>
    </div>
  </>;
}

function Exercise({ api, practice, item }: { api: Api; practice: string; item: PracticeItem }) {
  const [answer, setAnswer] = useState("");
  const [result, setResult] = useState<AnswerResult | null>(item.result);
  const [hint, setHint] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const path = `/practice-sets/${practice}/items/${item.id}`;

  async function act(operation: "answer" | "hint") {
    setBusy(true);
    setError("");
    try {
      if (operation === "answer") setResult(await api.json(`${path}/answer`, jsonBody("POST", { answer: answer.trim() })));
      else setHint((await api.json<{ hint: string }>(`${path}/hint`, { method: "POST" })).hint);
    } catch (reason) {
      setError(describeError(reason));
    } finally {
      setBusy(false);
    }
  }

  return <article className="panel exercise" id={`item-${item.id}`}>
    <p className="eyebrow section-code">练习 {String(item.position).padStart(2, "0")}</p>
    <h2>{item.stem}</h2>
    {result ? <div className={`answer-result ${result.correct ? "correct" : "incorrect"}`} role="status">
      <b>{result.correct ? "答对了" : "这题再看一步"}</b>
      <p>你的作答 <code>{result.answer}</code> · 核验结果 <code>{result.expected}</code></p>
      <p>{result.explanation}</p>
      <small>{result.eligibleForMastery ? "独立作答；同一道题的证据只记一次。" : "看过提示，这次作答用于练习，不计入掌握度。"}</small>
    </div> : <form onSubmit={(event) => { event.preventDefault(); if (answer.trim()) void act("answer"); }}>
      {item.choices.length > 0 ? <fieldset className="options"><legend className="small muted">选一个答案</legend>
        {item.choices.map((option, index) => {
          const letter = String.fromCharCode(65 + index);
          return <label key={letter}><input type="radio" name={`answer-${item.id}`} value={letter}
            checked={answer === letter} onChange={() => setAnswer(letter)} disabled={busy} /> {letter}. {option}</label>;
        })}
      </fieldset> : <div className="inline-form">
        <label htmlFor={`answer-${item.id}`}>你的作答</label>
        <input id={`answer-${item.id}`} aria-label={`第 ${item.position} 题作答`} value={answer} maxLength={96}
          autoComplete="off" spellCheck={false} disabled={busy} required
          onChange={(event) => setAnswer(event.target.value)} placeholder="例如 -2/3 或 x=4" />
      </div>}
      {hint && <p className="callout" role="status">{hint} <span className="small">本题不再计入掌握度。</span></p>}
      {error && <Problem>{error}</Problem>}
      <div className="actions">
        <button className="button primary" disabled={busy || !answer.trim()}>提交作答</button>
        <button className="text-button" type="button" disabled={busy} onClick={() => void act("hint")}>
          {item.hintSeen || hint ? "再看提示" : "看提示"}
        </button>
      </div>
    </form>}
  </article>;
}

export function PracticeView({ api, id, focus, back, mastery }: {
  api: Api; id: string; focus: string | null; back: () => void; mastery: () => void;
}) {
  const practice = useRemote<PracticeDetail>(api, `/practice-sets/${id}`, pending);
  useEffect(() => {
    if (focus && practice.data?.status === "done") document.getElementById(`item-${focus}`)?.scrollIntoView();
  }, [focus, practice.data]);
  return <>
    <div className="page-title">
      <button className="text-button back" onClick={back}>‹ 回到练习台</button>
      <p className="eyebrow section-code">草稿 · 独立作答</p>
      <h1>{practice.data?.purpose === "diagnose" ? "诊断练习" : "巩固练习"}</h1>
      <p className="muted">每题只提交一次。提交前可看提示，但使用提示的题不计入掌握度。</p>
    </div>
    {practice.error && <Problem retry={practice.reload}>{practice.error}</Problem>}
    {!practice.data && !practice.error && <p role="status">正在读取练习…</p>}
    {practice.data && pending(practice.data) && <Progress record={practice.data} type="practice" />}
    {practice.data?.status === "failed" && <Problem>{practice.data.error ?? "没有生成通过核验的练习，请重试。"}</Problem>}
    {!!practice.data?.items.length && <div className="exercise-grid">{practice.data.items.map((item) =>
      <Exercise key={item.id} api={api} practice={id} item={item} />)}</div>}
    {practice.data?.status === "done" && <button className="button secondary end-link" onClick={mastery}>
      查看知识点地图 <Icon name="arrow" size={16} />
    </button>}
  </>;
}
