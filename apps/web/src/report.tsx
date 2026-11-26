import { useEffect, useState } from "react";
import type { FormEvent } from "react";
import {
  describeError, jsonBody, outcomeNames, pending, type Api, type GradedItem, type Outcome,
  type Report, type SubmissionDetail,
} from "./api";
import { dateText, Empty, Icon, Problem, Progress, useRemote } from "./ui";

const reasonNames: Record<string, string> = {
  ambiguous_image: "题图不完整或识别有歧义", incomplete_question: "缺少完整题目",
  source_mismatch: "转写与原图不一致", model_disagreement: "独立解题结论冲突",
  missing_information: "题目缺少条件或存在歧义", missing_review: "独立复核结果不完整",
  answer_unclear: "作答无法可靠识别", mark_mismatch: "留白标记与作答矛盾",
  tool_failure: "计算工具未完成核验", tool_limit: "计算资源预算不足",
  invalid_tool_evidence: "计算证据无法复现", invalid_review: "复核题号不一致",
  invalid_partial: "部分正确缺少步骤依据", review_disagreement: "判分与等价性检查冲突",
  review_inconclusive: "复核尚无可靠结论", verification_failed: "未获得可靠核验结果",
};

function OriginalPage({ api, id, page, item }: { api: Api; id: string; page: number; item?: GradedItem }) {
  const [src, setSrc] = useState("");
  const [error, setError] = useState("");
  useEffect(() => {
    const abort = new AbortController();
    let objectUrl: string | undefined;
    setSrc("");
    setError("");
    api.blob(`/submissions/${id}/pages/${page}`, abort.signal).then((blob) => {
      if (!abort.signal.aborted) {
        objectUrl = URL.createObjectURL(blob);
        setSrc(objectUrl);
      }
    }).catch((reason) => { if (!abort.signal.aborted) setError(describeError(reason)); });
    return () => { abort.abort(); if (objectUrl) URL.revokeObjectURL(objectUrl); };
  }, [api, id, page]);
  const region = item?.region;
  if (error) return <Problem>{error}</Problem>;
  if (!src) return <p className="muted" role="status">正在读取原图…</p>;
  return <div className="page-image">
    <img src={src} alt={`作业第 ${page} 页原图`} />
    {region && <div className="annotation" aria-hidden="true" style={{
      left: `${region.left / 10}%`, top: `${region.top / 10}%`,
      width: `${(region.right - region.left) / 10}%`, height: `${(region.bottom - region.top) / 10}%`,
    }}><span>第 {item.label} 题</span></div>}
  </div>;
}

function Transcription({ api, id, item, revision, reload }: {
  api: Api; id: string; item: GradedItem; revision: number; reload: () => void;
}) {
  const [editing, setEditing] = useState(false);
  const [answer, setAnswer] = useState(item.answer ?? "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function save(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      await api.json(`/submissions/${id}/items/${item.id}/transcription`,
        jsonBody("PATCH", { answer: answer.trim(), revision }));
      setEditing(false);
      reload();
    } catch (reason) {
      setError(describeError(reason));
    } finally {
      setBusy(false);
    }
  }

  if (!editing) return <button className="text-button" onClick={() => setEditing(true)}>核对转写</button>;
  return <form className="inline-form" onSubmit={(event) => void save(event)}>
    <label htmlFor="transcription">按原图写出你的作答</label>
    <p className="small muted">改动后的结果仅供参考，不计入知识点掌握度。</p>
    <input autoFocus id="transcription" value={answer} autoComplete="off" maxLength={96}
      onChange={(event) => setAnswer(event.target.value)} required />
    {error && <Problem retry={reload}>{error}</Problem>}
    <div className="actions">
      <button className="button primary" disabled={busy || !answer.trim()}>核对结果</button>
      <button className="button secondary" type="button" onClick={() => { setEditing(false); setError(""); }}>取消</button>
    </div>
  </form>;
}

export function ReportView({ api, id, initialItem, back, practice }: {
  api: Api; id: string; initialItem: string | null; back: () => void; practice: (skill: string) => void;
}) {
  const report = useRemote<SubmissionDetail>(api, `/submissions/${id}`, pending);
  const [selected, setSelected] = useState<string | null>(initialItem);
  const [pageOverride, setPageOverride] = useState<number | null>(null);
  const [filter, setFilter] = useState<Outcome | null>(null);
  const [retrying, setRetrying] = useState(false);
  const [retryError, setRetryError] = useState("");
  const data = report.data;
  const rows: Report["items"] = data?.report?.items ?? [];
  const visibleRows = filter ? rows.filter((row) => row.outcome === filter) : rows;
  const item = visibleRows.find((row) => row.id === selected) ?? (pageOverride ? undefined : visibleRows[0]);
  const page = pageOverride ?? item?.page ?? data?.pages[0];

  async function retry() {
    setRetrying(true);
    setRetryError("");
    try {
      await api.json(`/submissions/${id}/retry`, { method: "POST" });
      report.reload();
    } catch (reason) {
      setRetryError(describeError(reason));
    } finally {
      setRetrying(false);
    }
  }

  return <>
    <div className="page-title">
      <button className="text-button back" onClick={back}>‹ 回到作业台</button>
      <p className="eyebrow section-code">批改 · 对照原图</p>
      <h1>作业报告</h1>
      <p className="muted">{data && `${dateText(data.createdAt)} · `}用于学习反馈，不作为正式成绩。</p>
    </div>
    {report.error && <Problem retry={report.reload}>{report.error}</Problem>}
    {!data && !report.error && <p role="status">正在读取作业…</p>}
    {data && pending(data) && <Progress record={data} type="report" />}
    {data?.status === "failed" && <>
      <Problem>{data.error ?? "未完成分析，请重新上传或稍后重试。"}</Problem>
      {data.canRetry && <button className="button secondary end-link" onClick={() => void retry()} disabled={retrying}>
        重新分析
      </button>}
      {retryError && <Problem>{retryError}</Problem>}
    </>}
    {data?.report && <>
      <div className="grade-summary" aria-label="按批改结果筛选">
        {(Object.keys(outcomeNames) as Outcome[]).map((outcome) => <button key={outcome}
          aria-pressed={filter === outcome} className={`grade-chip ${outcome}`}
          onClick={() => {
            setFilter(filter === outcome ? null : outcome);
            const first = rows.find((row) => row.outcome === outcome);
            if (filter !== outcome && first) { setSelected(first.id); setPageOverride(null); }
          }}>
          <b>{data.report?.counts[outcome] ?? 0}</b>{outcomeNames[outcome]}
        </button>)}
      </div>
      {data.report.retakePages.length > 0 && <div className="notice advisory" role="status">
        第 {data.report.retakePages.join("、")} 页看不清或未识别到完整题目，未判分。请重拍后重新上传。
      </div>}
      <div className="report-grid">
        <section className="panel original" aria-labelledby="original-title">
          <div className="section-heading"><h2 id="original-title">原图</h2><span className="small muted">点题目可定位</span></div>
          <div className="tabs" aria-label="原图页码">{data.pages.map((number) => <button key={number}
            aria-pressed={page === number}
            onClick={() => { setPageOverride(number); setSelected(null); }}>第 {number} 页</button>)}</div>
          {page !== undefined && <OriginalPage key={page} api={api} id={id} page={page}
            item={item?.page === page ? item : undefined} />}
        </section>
        <section className="panel" aria-labelledby="grade-title">
          <div className="section-heading"><h2 id="grade-title">逐题批改</h2>
            {filter && <button className="text-button" onClick={() => setFilter(null)}>查看全部</button>}</div>
          {visibleRows.length === 0 &&
            <Empty title="这组没有题目">可切换筛选，或检查原图是否清楚完整。</Empty>}
          <ul className="item-list">
            {visibleRows.map((row) => <li key={row.id}>
              <button className={`report-row${item?.id === row.id ? " selected" : ""}`}
                aria-pressed={item?.id === row.id}
                onClick={() => { setSelected(row.id); setPageOverride(null); }}>
                <span className="item-index">{row.label}</span>
                <span className="item-stem">{row.stem || `第 ${row.label} 题`}</span>
                <span className={`pill ${row.outcome}`}>{outcomeNames[row.outcome]}</span>
              </button>
            </li>)}
          </ul>
          {item && <div className="detail-sheet">
            <strong>第 {item.label} 题 · {outcomeNames[item.outcome]}</strong>
            <p className="small muted">
              {item.verification === "ai_review" ? "AI 独立解题与复核"
                : item.verification === "unresolved" ? "尚未完成可靠核验" : "确定性规则核验"}
              {item.toolChecked && " · 计算工具结果已复算"}
            </p>
            {item.stem && <p>{item.stem}</p>}
            {!!item.choices.length && <p className="choices-read">{item.choices.map((choice, index) =>
              <span key={index}>{String.fromCharCode(65 + index)}. {choice}</span>)}</p>}
            <dl className="facts">
              <div><dt>识别出的作答{item.corrected && "（已核对）"}</dt><dd>{item.answer ?? "—"}</dd></div>
              {item.expected && <div><dt>{item.verification === "ai_review" ? "AI 复核参考答案" : "核验结果"}</dt>
                <dd>{item.expected}</dd></div>}
            </dl>
            <p>{item.feedback}</p>
            {!!item.stepFeedback?.length && <ul>{item.stepFeedback.map((text, index) =>
              <li key={index}>{text}</li>)}</ul>}
            {!!item.knowledgeTitles?.length && <p className="small">关联知识点：{item.knowledgeTitles.join("、")}</p>}
            {item.reason && <p className="small muted">核验原因：{reasonNames[item.reason] ?? item.reason}</p>}
            {item.verification === "ai_review" && <p className="small muted">
              AI 复核不是数学证明，可能存在误判；此题不计入确定性掌握度。
            </p>}
            {item.corrected && <p className="small muted">此题为转写核对结果，未计入掌握度。</p>}
            <div className="actions">
              {item.canCorrect && <Transcription key={`${item.id}-${data.revision}`} api={api}
                id={id} item={item} revision={data.revision} reload={report.reload} />}
              {item.skill && <button className="text-button" onClick={() => practice(item.skill!)}>
                练同类题 <Icon name="arrow" size={15} />
              </button>}
            </div>
          </div>}
        </section>
      </div>
    </>}
  </>;
}
