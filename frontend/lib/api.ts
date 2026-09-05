export const API = "/backend";

export type Programme = { code: string; name: string };

export type MappingRow = {
  mapping_id: number;
  host_module_code: string;
  host_module_title: string;
  ntu_module_code: string;
  ntu_module_title: string;
  ntu_module_type: string;
  credits: number;
  year: string;
  sem: string;
  has_details: boolean;
};

export type UniversityCard = {
  university_id: number;
  name: string;
  country: string;
  city_state?: string | null;
  approved_count: number;
  programme_type: string;
  mappings_preview: MappingRow[];
  preview_shown: number;
};

export type UniversitiesPage = {
  cards: UniversityCard[];
  has_more: boolean;
  total: number;
  offset: number;
  // What this page does not cover. Under a CGPA filter the backend checks a
  // bounded pool of candidates, and says so here rather than reporting the
  // eligible count it found as if it were the count overall.
  notes?: string[];
};

export type ModuleConversion = {
  host_module_code: string;
  host_module_title: string;
  ntu_module_code: string;
  mapped_au: number;
  host_credits: number | null;
  host_credits_au: number | null;
};

export type CostOfLiving = {
  city?: string | null;
  summary?: string | null;
  vs_singapore?: string | null;
  single_person_monthly?: string | null;
  monthly_total_sgd?: number | null;
  rent_included?: boolean | null;
  items?: { label: string; price: string; amount_sgd?: number | null }[];
  url?: string | null;
  source?: string | null;
  error?: string | null;
};

export type UniversityBriefing = {
  name: string;
  country?: string | null;
  gem_program?: string | null;
  term?: string | null;
  source?: string | null;
  course_load_raw?: string | null;
  au_summary?: string | null;
  min_ects?: number | null;
  max_ects?: number | null;
  min_au?: number | null;
  max_au?: number | null;
  au_note?: string;
  planning_reference?: string | null;
  gpa?: string | null;
  brochure_url?: string | null;
  error?: string | null;
  cost_of_living?: CostOfLiving | null;
  module_conversions?: ModuleConversion[];
};

export type Conversion = {
  kind: string;
  summary: string;
  details: Record<string, unknown>;
};

export type Confidence = "high" | "medium" | "low";
export type Intent =
  | "core_planning"
  | "research"
  | "mixed"
  | "official_docs"
  | "general_questions"
  | "unknown";

export type SourceEvidence = {
  title: string;
  url: string;
  type:
    | "coursefinder"
    | "gem_explorer"
    | "ntu_intranet"
    | "official"
    | "web"
    | "reddit"
    | "wise"
    | "local"
    | "unknown";
  excerpt: string;
  retrieved_at: string;
  confidence: Confidence;
};

export type PlannedTask = {
  kind:
    | "profile"
    | "course_matching"
    | "workload"
    | "finance"
    | "research"
    | "conversion"
    | "official_docs"
    | "general_questions"
    | "clarification";
  status: "pending" | "running" | "complete" | "skipped" | "failed";
  description: string;
  warnings: string[];
};

export type WorkloadEvidence = {
  university_name: string;
  native_unit_text: string;
  unit_label?: string | null;
  minimum_value?: number | null;
  maximum_value?: number | null;
  module_count_minimum?: number | null;
  module_count_maximum?: number | null;
  raw_source_excerpt: string;
  source_url: string;
  retrieved_at: string;
  conversion_status: "not_applicable" | "supported" | "unsupported" | "unknown";
  conversion_basis?: {
    from_unit: string;
    to_unit: string;
    factor: number;
    source_excerpt: string;
    source_url?: string | null;
  } | null;
  planning_reference?: string | null;
  parse_warnings: string[];
};

export type FinanceEvidence = {
  university_name?: string | null;
  label: string;
  amount?: number | null;
  currency?: string | null;
  period?: string | null;
  includes_rent?: boolean | null;
  included_items: string[];
  excluded_items: string[];
  raw_source_excerpt: string;
  source?: SourceEvidence | null;
  parse_warnings: string[];
};

export type BudgetEstimate = {
  university_name?: string | null;
  components: {
    label: string;
    amount?: number | null;
    currency?: string | null;
    period: string;
    source_title?: string | null;
    included: boolean;
  }[];
  monthly_total?: number | null;
  monthly_total_currency?: string | null;
  rent_included?: boolean | null;
  fallback_used: boolean;
  source_label?: string | null;
  caveats: string[];
};

export type EligibilityEvidence = {
  university_name: string;
  requirement: string;
  required_value?: number | null;
  student_value?: number | null;
  /** Tri-state on purpose: null means the requirement is not published, which
   *  never removes a university from the shortlist. */
  meets?: boolean | null;
  source?: SourceEvidence | null;
  notes: string[];
};

export type ResearchFinding = {
  question: string;
  claim: string;
  source: SourceEvidence;
  confidence: Confidence;
  caveats: string[];
};

export type AnswerEnvelope = {
  answer: {
    summary: string;
    body: string;
    recommendations: string[];
    next_questions: string[];
  };
  sources: SourceEvidence[];
  confidence: Confidence;
  caveats: string[];
  intent: Intent;
  profile?: Profile | null;
  planned_tasks: PlannedTask[];
  universities: Record<string, unknown>[];
  workload_evidence: WorkloadEvidence[];
  finance_evidence: FinanceEvidence[];
  eligibility_evidence: EligibilityEvidence[];
  budget?: BudgetEstimate | null;
  budgets: BudgetEstimate[];
  research_findings: ResearchFinding[];
  conversion_results: Record<string, unknown>[];
  clarification?: string | null;
  errors: { code: string; message: string; details: Record<string, unknown> }[];
};

export type Profile = {
  school_code: string;
  school_name: string;
  preferred_semester: string;
  programme_type: string;
  destination_pref?: string | null;
  destination_region?: string | null;
  destination_countries?: string[];
  module_codes?: string[];
  excluded_module_types?: string[];
  included_module_types?: string[];
  restored_module_types?: string[];
  cgpa?: number | null;
  max_monthly_budget_sgd?: number | null;
};

export type Payload = {
  answer?: AnswerEnvelope["answer"];
  sources?: SourceEvidence[];
  confidence?: Confidence;
  caveats?: string[];
  intent?: Intent;
  planned_tasks?: PlannedTask[];
  universities?: Record<string, unknown>[];
  workload_evidence?: WorkloadEvidence[];
  finance_evidence?: FinanceEvidence[];
  eligibility_evidence?: EligibilityEvidence[];
  budget?: BudgetEstimate | null;
  budgets?: BudgetEstimate[];
  research_findings?: ResearchFinding[];
  conversion_results?: Record<string, unknown>[];
  errors?: AnswerEnvelope["errors"];
  clarification?: string | null;
  narration?: string;
  cards?: UniversityCard[];
  total_universities?: number;
  conversions?: Conversion[];
  research?: string | null;
  profile?: Profile | null;
  has_more?: boolean;
};

export type ChatMessage = {
  role: "user" | "assistant";
  content: string;
  payload?: Payload;
};

export type ChatSummary = {
  session_id: string;
  title: string;
  folder: string | null;
  created_at: string;
  updated_at: string;
};

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(init?.headers || {}),
    },
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(text || res.statusText);
  }
  return res.json() as Promise<T>;
}

/** One server-sent event from POST /api/chat/stream. */
export type StreamEvent =
  | { type: "open"; session_id: string }
  | { type: "plan"; planned_tasks: PlannedTask[]; intent?: Intent }
  | { type: "task"; kind: PlannedTask["kind"]; status: PlannedTask["status"]; description: string; warnings: string[] }
  | { type: "done"; payload: Payload & { session_id: string; messages: ChatMessage[] } }
  | { type: "error"; message: string };

/**
 * Run one turn with live progress.
 *
 * The plan is the interesting part: the router commits to a set of tasks before
 * any lane runs, so the student watches the system decide what to do and then
 * do it. A caller that ignores every intermediate event still receives the
 * complete answer in `done`, which is why the non-streaming endpoint remains
 * the fallback rather than a second code path to keep in sync.
 */
export async function streamChat(
  body: { message: string; session_id?: string | null },
  onEvent: (event: StreamEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  const res = await fetch(`${API}/api/chat/stream`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });
  if (!res.ok || !res.body) {
    throw new Error((await res.text()) || res.statusText);
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    // Frames are separated by a blank line; anything after the last one is a
    // partial frame and must stay in the buffer.
    const frames = buffer.split("\n\n");
    buffer = frames.pop() ?? "";

    for (const frame of frames) {
      let name = "";
      let data = "";
      for (const line of frame.split("\n")) {
        if (line.startsWith("event:")) name = line.slice(6).trim();
        else if (line.startsWith("data:")) data += line.slice(5).trim();
      }
      if (!name || !data) continue;
      try {
        const parsed = JSON.parse(data);
        onEvent(name === "done" ? { type: "done", payload: parsed } : { type: name, ...parsed });
      } catch {
        // A malformed frame loses one update, not the whole turn.
      }
    }
  }
}
