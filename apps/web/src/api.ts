export type Status = "queued" | "running" | "done" | "failed";
export type Outcome = "correct" | "partial" | "incorrect" | "blank" | "undetermined";
export type MasteryStatus = "insufficient" | "priority" | "reinforce" | "stable";
export type Difficulty = "foundation" | "standard";

export type SessionConfig =
  | { mode: "local"; demoStudents: string[]; sampleOnly: boolean }
  | { mode: "entra"; tenantId: string; clientId: string; scope: string };

export interface JobRecord {
  id: string;
  jobId: string;
  createdAt: number;
  status: Status;
  error: string | null;
}

export interface Submission extends JobRecord {
  revision: number;
  canRetry: boolean;
}

export interface Region {
  left: number;
  top: number;
  right: number;
  bottom: number;
}

export interface GradedItem {
  id: string;
  page: number;
  label: string;
  region: Region;
  stem: string;
  choices: string[];
  answer: string | null;
  corrected: boolean;
  expected: string | null;
  skill: string | null;
  outcome: Outcome;
  feedback: string;
  canCorrect: boolean;
  verification?: "rules" | "ai_review" | "unresolved";
  reason?: string | null;
  toolChecked?: boolean;
  eligibleForMastery?: boolean;
  knowledgeIds?: string[];
  knowledgeTitles?: string[];
  stepFeedback?: string[];
}

export interface Report {
  items: GradedItem[];
  counts: Record<Outcome, number>;
  retakePages: number[];
}

export interface SubmissionDetail extends Submission {
  pages: number[];
  report: Report | null;
}

export interface Evidence {
  itemId: string;
  submissionId: string | null;
  practiceId: string | null;
  englishId?: string | null;
  score: number;
  occurredAt: number;
}

export interface Mastery {
  id: string;
  unit: string;
  title: string;
  status: MasteryStatus;
  percent: number | null;
  sampleCount: number;
  evidence: Evidence[];
}

export interface PracticeSet extends JobRecord {
  skill: string;
  purpose: "diagnose" | "strengthen";
  difficulty: Difficulty;
  count: number;
}

export interface AnswerResult {
  answer: string;
  correct: boolean;
  expected: string;
  explanation: string;
  eligibleForMastery: boolean;
}

export interface PracticeItem {
  id: string;
  position: number;
  stem: string;
  choices: string[];
  hintSeen: boolean;
  result: AnswerResult | null;
}

export interface PracticeDetail extends PracticeSet {
  items: PracticeItem[];
}

export function describeError(error: unknown) {
  return error instanceof Error ? error.message : "操作未完成，请稍后重试。";
}

export function apiClient(authHeaders: () => Promise<Record<string, string>> = async () => ({})) {
  async function send(path: string, init: RequestInit) {
    const headers = new Headers(init.headers);
    for (const [name, value] of Object.entries(await authHeaders())) headers.set(name, value);
    let response: Response;
    try {
      response = await fetch(`/api${path}`, { ...init, headers, cache: "no-store" });
    } catch (error) {
      if (init.signal?.aborted) throw error;
      throw new Error("暂时连不上服务。检查网络后再试。");
    }
    if (!response.ok) {
      const body = await response.json().catch(() => null);
      throw new Error(typeof body?.detail === "string" ? body.detail : `请求失败（${response.status}）。请重试。`);
    }
    return response;
  }
  return {
    async json<T>(path: string, init: RequestInit = {}): Promise<T> {
      return (await send(path, init)).json();
    },
    async blob(path: string, signal?: AbortSignal) {
      return (await send(path, { signal })).blob();
    },
  };
}

export type Api = ReturnType<typeof apiClient>;

export function jsonBody(method: "POST" | "PATCH", value: object): RequestInit {
  return { method, body: JSON.stringify(value), headers: { "Content-Type": "application/json" } };
}

export const pending = (record: JobRecord) => record.status === "queued" || record.status === "running";

export const outcomeNames: Record<Outcome, string> = {
  correct: "正确", partial: "部分正确", incorrect: "错误", blank: "未作答", undetermined: "无法判定",
};

export const masteryNames: Record<MasteryStatus, string> = {
  insufficient: "证据不足", priority: "优先练习", reinforce: "巩固中", stable: "较稳定",
};
