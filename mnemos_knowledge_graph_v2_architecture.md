# Mnemos Knowledge Graph v2 — Entity & Organization Intelligence Architecture

**Internal architecture proposal · July 22, 2026**  
**Status:** design authority for the next decade of semantic memory  
**Grounded against:** shipped `nexus_v1` (`entities`, `relations`, `people`, People v2, Attention Field, Track C/F)

---

## 0. Thesis

Today’s graph answers *“what names did extraction emit?”* with thin nodes and mostly-relationship provenance. That was the right prototype: it proved typed edges, user-sovereign edits, rebuild discipline (`derived` wipe / `asserted`+`user` survive), and constellation as a *ranked projection* rather than a dump of the full graph.

It will not survive decades.

**v2 redesigns the graph as a temporal, evidence-backed belief store** — not a CRM, not a LinkedIn clone, not an embedding index with pretty nodes. Every durable claim is a *belief* with:

- a **canonical subject** (entity / person / typed node)
- a **typed predicate** with ontology constraints
- an **object**
- a **validity interval** (when it was true in the world)
- an **evidence set** (why we believe it)
- a **posterior confidence** (how strongly we believe it *now*)
- an **epistemic layer** (observed → asserted → derived → hypothesized → verified)

The Attention Field already taught us that gravity must not conflate strength, activation, urgency, and trust. The Knowledge Graph must learn the same lesson: **identity ≠ mention ≠ affiliation ≠ importance ≠ retrieval score.**

**Sacred inheritances from v1 (do not break):**

1. Ranked constellation projection, never raw graph viz as the primary UI  
2. User edits (`origin='user'`) outlive rebuilds and outweigh models (I-3)  
3. Span preservation and review-first truth (I-1, I-2)  
4. Local-first; personal graph never leaves the device as training fodder (I-8)  
5. Calm: graph maintenance must not interrupt (I-9)

---

## 1. Diagnosis — what breaks at decade scale

| Failure mode | v1 evidence | Decade consequence |
|---|---|---|
| Thin entity rows | `entities(id, name, kind, aliases, embedding, first/last_seen)` | No rebrand, no HQ, no external IDs, no lifecycle → every OCR variant becomes a node |
| Name-primary resolve | `resolve_entity` exact/fuzzy on `canonical_name` | OpenAI / Open AI / OpenAI Inc. either merge wrongly or fan out forever |
| Provenance on edges only | `relations.source_event_id` single pointer | Cannot answer “why?” with multi-evidence; entity itself is unexplained |
| Unique edge key | `(subj, pred, obj)` unique | New observation *updates* weight/confidence — history of belief is lost |
| Derived pollution | rebuild invents `associated_with` / `about` | Co-mention of “Dell” on a TMZ page contaminates Patrick’s neighborhood |
| No temporality | `last_seen` only | Cannot answer “where did Patrick work in 2024?” |
| Ontology too small | person / org / tool / project / idea / thing / place | Lifelong memory needs meetings, decisions, repos, investments, preferences… |
| `knowledge_entities` unused | ✅ fixed in KG-A — enforced on mint; bind-existing still allowed | — |
| Importance = gravity conflation | Field doc W1 | Org Intelligence and Tool Intelligence cannot specialize |

---

## 2. Layered architecture (recommended)

```
Perception events (append-only)
        │
        ▼
Extraction (LLM + deterministic extractors)
        │  emits Mentions + CandidateClaims (not graph mutations)
        ▼
┌─────────────────────────────────────────────┐
│  Claim Intake                               │
│  - source_policy gate                       │
│  - span faithfulness                        │
│  - epistemic tier assignment                │
└──────────────────┬──────────────────────────┘
                   ▼
┌─────────────────────────────────────────────┐
│  Entity Resolution Service                  │
│  block → candidates → score → decide        │
│  (auto / review / leave_open / reject)      │
└──────────────────┬──────────────────────────┘
                   ▼
┌─────────────────────────────────────────────┐
│  Belief Store (temporal KG)                 │
│  nodes + predicates + evidence bags            │
│  posterior confidence + validity intervals  │
└──────────────────┬──────────────────────────┘
                   ▼
┌─────────────────────────────────────────────┐
│  Materialized views                         │
│  Org Intelligence / Tool Intelligence       │
│  Importance / Constellation seeds           │
│  Retrieval indexes (Lance + SQL)            │
└──────────────────┬──────────────────────────┘
                   ▼
Attention Field / Grounding / Agents / UI
```

**Critical separation:** extractors never write the belief store directly. They write **candidate claims**. The claim intake is the only mutator of durable beliefs (plus user edits). This is how Google KG and Palantir keep write fan-out under control.

---

## 3. Entity model (richer schema)

### 3.1 Unify under `kg_nodes`

v1 splits `people` and `entities`. Keep the *product* distinction (People tab vs Entities), but unify storage under one node table with a type taxonomy. People v2 columns already lean this way (`promotion_state`, `actor_type`, `public_figure`).

```sql
-- Canonical node (person | org | tool | …)
CREATE TABLE kg_nodes (
  id                INTEGER PRIMARY KEY,
  node_type         TEXT NOT NULL,          -- ontology class
  canonical_id      TEXT NOT NULL,          -- OPAQUE random 128-bit hex (see §4) — carries NO semantics, never parsed
  display_name      TEXT NOT NULL,
  aliases_json      TEXT NOT NULL DEFAULT '[]',
  description       TEXT,                   -- short, user-visible
  summary_md        TEXT,                   -- longer living brief (org/tool intel)
  -- Identity / external
  external_ids_json TEXT NOT NULL DEFAULT '{}',  -- {domain, linkedin, github, lei, …}
  primary_domain    TEXT,                   -- e.g. dell.com
  -- Semantics
  domains_json      TEXT NOT NULL DEFAULT '[]',  -- industry tags
  locations_json    TEXT NOT NULL DEFAULT '[]',
  -- Vectors
  embedding         BLOB,                   -- name+alias+desc embedding
  -- Trust & importance
  confidence        REAL NOT NULL DEFAULT 0.5,  -- identity confidence
  importance        REAL NOT NULL DEFAULT 0.0,  -- global prior (§11)
  user_importance   REAL NOT NULL DEFAULT 0.0,  -- pin / explicit boost
  popularity        REAL NOT NULL DEFAULT 0.0,  -- mention mass (decaying)
  -- Lifecycle
  lifecycle         TEXT NOT NULL DEFAULT 'new',
  protected         INTEGER NOT NULL DEFAULT 0, -- user/system protect
  merged_into_id    INTEGER,                -- soft merge target
  -- Temporal envelope of the *node's existence as known*
  first_seen        REAL NOT NULL,
  last_seen         REAL NOT NULL,
  valid_from        REAL,                   -- when entity began (org founded, …)
  valid_to          REAL,                   -- dissolved / deprecated
  -- Versioning
  schema_version    INTEGER NOT NULL DEFAULT 2,
  updated_at        REAL NOT NULL,
  UNIQUE(canonical_id)
);

-- Post-review Change 1: identity is opaque; NAMES are blocking keys only.
-- The same key_value MAY map to multiple nodes — that ambiguity is exactly
-- what the resolver adjudicates. Merges COPY the loser's keys to the winner;
-- key rows are never deleted. (Shipped in nexus_v1 as kg_node_keys with a
-- node_type column, since nodes still live in people/entities.)
CREATE TABLE kg_node_keys (
  node_id   INTEGER NOT NULL REFERENCES kg_nodes(id),
  key_type  TEXT NOT NULL,      -- norm_name | phonetic | domain | alias_norm
  key_value TEXT NOT NULL,
  created_at REAL NOT NULL,
  PRIMARY KEY (key_type, key_value, node_id)
);
CREATE INDEX idx_node_keys_lookup ON kg_node_keys(key_type, key_value);

-- Post-review Change 2: SQLite permits NULLs in ordinary-table PRIMARY KEYs
-- and treats them as distinct, so PRIMARY KEY(node_id, key, valid_from) with
-- nullable valid_from would NOT deduplicate atemporal attributes. Surrogate
-- id + expression unique index (NULL folded to the -1 sentinel) enforces
-- real uniqueness while queries keep readable NULL semantics.
CREATE TABLE kg_node_attrs (
  id          INTEGER PRIMARY KEY,
  node_id     INTEGER NOT NULL,
  key         TEXT NOT NULL,               -- typed attribute namespace
  value_json  TEXT NOT NULL,
  confidence  REAL NOT NULL,
  valid_from  REAL,                        -- NULL = atemporal ("always/unknown")
  valid_to    REAL,
  source      TEXT NOT NULL,               -- mined | user | asserted | derived
  evidence_id INTEGER,                     -- FK kg_evidence
  updated_at  REAL NOT NULL
);
CREATE UNIQUE INDEX uq_attr_temporal
  ON kg_node_attrs(node_id, key, ifnull(valid_from, -1));
```

### 3.2 Field rationale (why each exists)

| Field | Why | Populated by | Evolves how |
|---|---|---|---|
| `canonical_id` | Stable identity under rename/merge | Opaque random hex minted at create | Immutable; NEVER carries or acquires semantics (an OCR-garbled first mention can't poison it) — names live in `kg_node_keys` |
| `display_name` | UI / speech | Most frequent clean alias or user override | Updates on user edit or high-confidence rename claim |
| `aliases_json` | ASR/OCR variants | Every accepted mention | Append-only with decay of unused aliases |
| `description` / `summary_md` | Org/Tool Intelligence briefs | Reflection + deterministic rollups | Nightly refresh; user edits win |
| `external_ids_json` | Cross-source join (domain, LinkedIn) | Deterministic extractors, user, rare LLM | Only with evidence; conflicts → review |
| `primary_domain` | Blocking + merge signal | Email/URL extract | Strong merge feature |
| `embedding` | Soft candidates | Incremental embed job | Refresh on alias/desc change |
| `confidence` | Identity trust | Resolver posterior | Decays if never re-observed; rises on multi-source |
| `importance` / `user_importance` / `popularity` | Retrieval & constellation | Importance engine (§11) | Continuous; user pin is sovereign |
| `lifecycle` | GC without delete | Promotion / dormancy jobs | See §6 |
| `merged_into_id` | Soft merge | Resolver / user | Never hard-delete history |
| `valid_from` / `valid_to` | Entity existence in world | User / inferred | Rare; mostly for orgs/tools |
| `kg_node_attrs` | Typed facts about the node | Same claim pipeline | Temporal attributes (CEO, HQ, version) |

**People remain first-class** via `node_type='person'` plus People v2 mention ledger. Contact points stay on `person_contact_points` (already stronger than org attrs).

---

## 4. Canonical entity resolution (production-grade)

### 4.1 Problem with v1

Name equality (plus light fuzzy) is LinkedIn *display-name* matching, not entity resolution. False merges destroy trust; false splits destroy retrieval.

### 4.2 Pipeline (how Google/LinkedIn-class systems do it)

```
Mention
  → Normalize (case, corp suffixes Inc/LLC/Ltd, punctuation)
  → Block (cheap partitions)
  → Generate candidates (≤50)
  → Score (feature vector)
  → Decide (thresholds + human gate)
  → Act (bind / create / leave_open / suggest_merge)
```

**Blocking keys (union of blocks):**

1. Normalized exact name  
2. Phonetic / stripped-corp-suffix key (`openai`)  
3. `primary_domain` / email domain  
4. Embedding ANN (Lance, top-k=32)  
5. Shared neighborhood: people who `works_at` / `uses` both names  
6. Alias inverted index  

**Features (scored, not OR’d):**

| Feature | Weight class | Notes |
|---|---|---|
| Exact alias match | Hard positive | Auto-bind if unique in block |
| Domain match | Hard positive | `openai.com` ↔ OpenAI |
| Soft string (Jaro-Winkler / token Jaccard) | Medium | “Open AI” ↔ “OpenAI” |
| Embedding cosine | Medium | Disambiguates “Apple” fruit vs company with context |
| Neighborhood Jaccard | High | Same co-workers / same projects |
| Kind compatibility | Gate | Never auto-merge `person`↔`org` |
| Temporal overlap | Soft | Active in same years |
| Source policy class | Soft | News-only mentions should not create orgs lightly |

**Decision thresholds (shipped defaults, tunable per user later):**

- Auto-bind if score ≥ 0.92 and margin ≥ 0.08 and kind compatible  
- Suggest merge (review UI) if 0.78–0.92  
- Leave open if ambiguous top-2 within 0.05  
- Create new if score < 0.55 and policy allows `knowledge_entities`  
- Reject if policy denies or public-figure heuristic on news-only evidence  

**Split detection:** **deferred — post-PMF** (post-review Change 7). Automated disjoint-cluster detection is expensive at scale and its failure mode is fully recoverable by hand because merges are soft and evidence is append-only. Shipped instead: a Memory Console **"Split node"** action (`POST /kg/split`) — mint a fresh opaque-id node, reassign chosen beliefs (each carries its evidence bag), recompute both posteriors, log `split_accept` in `kg_adjudications`.

**Human confirmation:** Memory Console “Identity” queue — same spirit as People v2 unresolved mentions. Org/tool merges are review-first when either node is `trusted` or `protected`.

### 4.3 Pseudocode

```text
function resolve(mention, context):
  policy = source_policy(context)
  if not policy.knowledge_entities: return Reject

  key = normalize(mention.text)
  blocks = union(block_exact(key), block_domain(context),
                 block_ann(embed(mention)), block_graph(context))
  cands = unique(blocks)[:50]
  scored = [(c, score(mention, c, context)) for c in cands]
  sort scored desc

  if scored[0].score >= AUTO and margin(scored) >= MARGIN:
    return Bind(scored[0].id, scored[0].score)
  if scored[0].score >= REVIEW:
    return SuggestMerge(...)
  if policy.create_entities and relevance(context) >= CREATE:
    return Create(canonical_key=key, ...)
  return LeaveOpen
```

### 4.4 Trade-offs

| Approach | Pros | Cons | Verdict |
|---|---|---|---|
| Name-only (v1) | Simple | Decade failure | Reject |
| Pure embedding cluster | Soft recall | False merges (“Apple”) | Assist only |
| Blocking + features + human gate | Explainable, scalable | Engineering cost | **Adopt** |
| External KG link (Wikidata) | Disambiguation | Privacy / offline | Optional later, local cache only |

---

## 5. Confidence architecture

Nothing binary. Every **predicate** (belief) has a posterior; every **evidence** has a likelihood.

### 5.1 Model

For belief \(b\) (subject, predicate, object, validity window):

\[
\log \frac{P(b)}{1-P(b)} = \text{prior} + \sum_i w(s_i)\, \ell_i - \lambda_{\text{conflict}}\,C\,\mathbb{1}[\text{simultaneous}] - \delta(t)
\]

- \(w(s_i)\): source weight (user assertion ≫ email signature ≫ meeting ASR ≫ news OCR)  
- \(\ell_i\): evidence log-likelihood (span faithfulness, extractor conf, recency)  
- \(C\): conflict mass from competing beliefs (Patrick works_at X vs Y overlapping intervals)  
- \(\delta(t)\): time decay on *identity of belief* if never reconfirmed (slow; not the same as Field activation decay)

**Source weights (initial table, data-driven later):**

| Source class | \(w\) |
|---|---|
| User assert / pin | 10.0 |
| Email signature / calendar organizer | 3.0 |
| Private conversation / meeting | 2.0 |
| User document | 1.8 |
| Screen (non-news) | 1.0 |
| News / social browse | 0.2 |
| Derived co-mention | 0.1 |
| Hypothesized / predicted | 0.05 |

The source-weight table lives in the versioned `kg_config` row `source_weights` (post-review Change 4), not in code — fitted weights ship without a redeploy; a version bump forces a full posterior re-scan.

**Lazy recomputation (post-review Change 5):** intake never does posterior math — evidence insert appends the row and flips `posterior_stale`. Recompute happens on read (`/kg/explain`, retrieval) or in the capped `kg_confidence_recal` batch sweep (boot + on demand). Since the model is a sum of per-evidence terms, the time-invariant sum is cached on the predicate (`logit_sum`); \(\delta(t)\) depends on `now`, so no "final" posterior is ever stored — most reads only re-apply decay. Full re-scan only on retroactive term changes (evidence rejection, weight-table version bump).

**Accumulation:** evidence is append-only. New supporting evidence increases posterior; it never “overwrites.”  
**Conflicts (temporal-split-first, post-review Change 3):** classify before penalizing. *Sequential* (new belief's earliest evidence postdates the old belief's latest by > `SEQ_GAP_DAYS`=14 and the new posterior-without-penalty ≥ `SPLIT_MIN_CONF`=0.6) → NO penalty on either side; auto-generate the temporal split (`old.valid_to = new.valid_from`, supersede) — auto-applied unless the old belief is trusted/protected, in which case the split is enqueued for review pre-filled as the default action. *Simultaneous* (overlapping evidence windows) → symmetric penalty + adjudication, with a "both true" resolution that clears the flag and restores posteriors. The system must not get LESS confident precisely when it learns something new.  
**Manual overrides:** user belief locks posterior to 1.0 (or 0.0 if rejected) and sets `protected`.  
**Graph propagation:** *do not* blindly propagate confidence to neighbors (rumor mill). Only allow typed, capped boosts (e.g., email domain → org identity +0.05) logged as derived evidence.

### 5.2 Evolution over years

- Posteriors without fresh evidence drift toward prior slowly (half-life years, not days).  
- Importance/popularity decay faster (months).  
- Historical beliefs keep high confidence *inside their validity interval* even if superseded for “today.”

---

## 6. Provenance system

### 6.1 Problem

v1: one `source_event_id` on a relation. Cannot show eight supporting observations or mixed modalities.

### 6.2 Design: evidence bags

```sql
CREATE TABLE kg_predicates (
  id              INTEGER PRIMARY KEY,
  subj_type       TEXT NOT NULL,
  subj_id         INTEGER NOT NULL,
  predicate       TEXT NOT NULL,
  obj_type        TEXT NOT NULL,
  obj_id          INTEGER NOT NULL,
  -- Epistemic
  layer           TEXT NOT NULL,  -- observed|asserted|derived|hypothesized|predicted|user_verified
  confidence      REAL NOT NULL,
  -- Temporal (world time)
  valid_from      REAL,
  valid_to        REAL,
  -- Observation envelope
  first_seen      REAL NOT NULL,
  last_seen       REAL NOT NULL,
  -- Lifecycle
  status          TEXT NOT NULL DEFAULT 'active', -- active|superseded|retracted|archived
  superseded_by   INTEGER,
  protected       INTEGER NOT NULL DEFAULT 0,
  created_at      REAL NOT NULL,
  updated_at      REAL NOT NULL
);

-- Many evidence rows per predicate (THE provenance spine)
CREATE TABLE kg_evidence (
  id              INTEGER PRIMARY KEY,
  predicate_id    INTEGER NOT NULL,
  event_id        INTEGER,          -- perception event
  fact_id         INTEGER,          -- optional fact bridge
  modality        TEXT,             -- audio|screen|email|calendar|doc|user|…
  source_class    TEXT,             -- source_policy class
  quote           TEXT,             -- verbatim span (I-1)
  quote_hash      TEXT,
  extractor_conf  REAL,
  faithfulness    REAL,
  observed_at     REAL NOT NULL,
  weight          REAL NOT NULL,    -- w(s) at write time
  created_by      TEXT NOT NULL,    -- system|user|reasoner
  meta_json       TEXT
);

CREATE INDEX idx_ev_pred ON kg_evidence(predicate_id, observed_at DESC);
CREATE INDEX idx_pred_subj ON kg_predicates(subj_type, subj_id, status);
CREATE INDEX idx_pred_temporal ON kg_predicates(predicate, valid_from, valid_to);
```

**UI:** Evidence drawer (already in constellation) upgrades from “one event snippet” to **ranked evidence list** with modality icons, dates, and “confirm / reject” (feeds confidence). Chat answers that cite affiliations attach the same bag via grounding sources.

**Why belief ≠ evidence:** so we can retract one bad OCR quote without deleting the affiliation if seven other evidences remain.

---

## 7. Temporal knowledge graph

### 7.1 Semantics

- `valid_from` / `valid_to`: when the *world fact* held (open `valid_to` = current).  
- `first_seen` / `last_seen`: when *Mnemos observed* it.  
- `status=superseded`: replaced by another predicate id (job change).  

**Query examples:**

```sql
-- Where did Patrick work in 2024?
SELECT * FROM kg_predicates
WHERE subj_id=:patrick AND predicate='works_at'
  AND status IN ('active','superseded')
  AND valid_from < :end_2024
  AND (valid_to IS NULL OR valid_to > :start_2024);

-- Tools Justin stopped using
SELECT * FROM kg_predicates
WHERE subj_id=:justin AND predicate='uses'
  AND valid_to IS NOT NULL AND valid_to < :now
  AND status='superseded';
```

**Inference of intervals:** if new `works_at B` arrives with high confidence while `works_at A` is open, system proposes `A.valid_to = B.valid_from` (review if both trusted). Never silent overwrite.

---

## 8. Entity lifecycle

| State | Meaning | Entry | Exit |
|---|---|---|---|
| `new` | Minted, thin evidence | create | +evidence → active |
| `active` | In circulation | evidence / use | dormancy job |
| `trusted` | Multi-source or user-verified | promotion gate | demotion rare |
| `dormant` | No observations > T | nightly job | resurrection on mention |
| `archived` | Hidden from default retrieval | user / GC suggest | restore |
| `merged` | `merged_into_id` set | resolver / user | — |
| `deleted` | Tombstone only (soft) | user | undelete |
| `protected` | Flag, not state | user / system | — |

**GC:** never hard-delete nodes with evidence. Archive + drop from constellation candidates. **Resurrection:** new mention of archived alias reopens as `active` with prior evidence intact.

**Promotion (org/tool):** analogous to People v2 — `new → active` on first non-news evidence; `active → trusted` on ≥2 source classes or user verify.

---

## 9. Rich ontology

### 9.1 Core classes (v2.0 ship set)

**Agents & social:** Person, Team, Organization, Department  
**Work product:** Project, Goal, Decision, Commitment, Task, Habit, Preference  
**Artifacts:** Document, Repository, Dataset, Meeting, Conversation, MemoryEpisode  
**Tech:** Tool, Technology, API, Device, Workflow  
**World:** Location, Product, Concept, Question  
**Capital (optional module):** Investor, Investment, Startup, Customer, Competitor  

Do **not** ship all classes day one. Ship the core set with an extension registry (`ontology_version`, class JSON schema). Empty classes with no extractors are worse than absent classes.

### 9.2 Relationship taxonomy (selected)

| Predicate | Domain → Range | Card. | Notes |
|---|---|---|---|
| `works_at` | Person → Org | many-to-one *current* | Temporal; role attr |
| `member_of` | Person → Team | many | |
| `reports_to` | Person → Person | many-to-one | Sensitive; user-verify bias |
| `uses` | Person\|Project → Tool | many | Usage strength in attrs |
| `built_with` | Project → Tool\|Tech | many | |
| `depends_on` | Tool\|Project → Tool\|API | many | |
| `customer_of` / `partner_with` / `competitor_of` | Org → Org | many | High bar for auto |
| `founded` / `invested_in` | Person\|Org → Org | many | Capital module |
| `about` | Fact\|Doc\|Meeting → Node | many | Derived allowed |
| `evidenced_by` | Predicate → Event | many | Via evidence table |
| `supersedes` | Predicate → Predicate | one | Temporal chain |

**Directionality:** always stored directed; inverse views are query-time.  
**Versioning:** new predicate row + supersession, never mutate predicate type in place.

---

## 10. Derived vs asserted — epistemic layers

| Layer | Writer | May auto-apply to retrieval? | May show in constellation? |
|---|---|---|---|
| `user_verified` | Human | Yes | Yes |
| `asserted` | Extractor typed relation with span | Yes if conf≥τ | Yes |
| `observed` | Deterministic (email domain, calendar) | Yes | Yes |
| `derived` | Rebuild heuristics | **Only as soft features** | No (or ghost) |
| `hypothesized` | Reasoners | No | No |
| `predicted` | Track F predictors | No | Horizon only |

**Rule:** derived co-mention must **not** create `works_at`. It may boost *candidate score* for an asserted claim or seed a hypothesized affiliation for review.

**Promotion path:** hypothesized → (user confirm) → user_verified; or hypothesized → (multi-source observed) → asserted.

This is how we stop hallucinated graph growth while still allowing AI to *suggest*.

---

## 11. Evidence accumulation

One observation never overwrites another. Algorithm:

1. Resolve subject/object nodes.  
2. Find open predicate matching (s, p, o) with overlapping validity *or* create new.  
3. Insert `kg_evidence` row (dedupe by `quote_hash` + `event_id`).  
4. Flag `posterior_stale` (recompute is lazy — §5, post-review Change 5).  
5. If conflict with another object for same (s, p) open interval → classify sequential vs simultaneous (§5); sequential auto-splits, simultaneous penalizes + enqueues adjudication.
6. **Log every adjudication** (post-review Change 4) into `kg_adjudications`: evidence confirm/reject, merge accept/reject, split accept/reject, conflict calls, belief locks — human AND auto decisions — each with the FROZEN feature vector the scorer saw at decision time (`features_json`: per-evidence source_class/w/extractor_conf/faithfulness/recency/modality + posterior-before + conflict class) and `model_score`. This is the free labeling stream that turns hand-tuned source weights into fitted ones. LOCAL-ONLY: the table is on the export/telemetry denylist (I-8). The fitting job itself is deliberately NOT built yet — instrumentation only.

**Patrick works_at Dell** with email + meeting + LinkedIn + calendar + resume: five evidences, high \(w\), posterior → 0.98.  
One TMZ OCR “Dell” next to Patrick’s name: evidence with \(w=0.2\); alone cannot mint; with existing belief, tiny bump only if span supports *employment*.

---

## 12. Importance ranking

Separate from confidence and from Field activation.

\[
I = \sigma\big(\alpha_1 \log(1+m) + \alpha_2 r + \alpha_3 c + \alpha_4 u + \alpha_5 p\big)
\]

- \(m\): decaying mention mass  
- \(r\): recency of last meaningful interaction (meeting/email/task, not news)  
- \(c\): connected trusted people count  
- \(u\): `user_importance` / pin  
- \(p\): open projects/commitments involving node  

**Consumers:**

- **Retrieval:** multiply semantic score by \(I^\beta\) and \(confidence^\gamma\)  
- **Constellation:** candidate prior (Field already has centrality — replace/extend with \(I\))  
- **Reflection:** prefer high-\(I\) orgs/tools in daily briefs  
- **Agents:** planning context packs high-\(I\) tools for “how Justin works”

---

## 13. Organization Intelligence

An Organization page is a **materialized living brief**, not a CRM record.

Auto-accumulate (read models, refresh nightly + on event):

- People (current / former via temporal `works_at`)  
- Projects, tools, meetings, docs touching the org  
- Open commitments involving affiliated people  
- Competitors/partners (only asserted/user)  
- Timeline of belief changes  
- Confidence + evidence summary  

**≠ CRM:** no pipeline stages, no owned “deals” unless user creates them. Mnemos remembers *your* relationship to the org across modalities. Salesforce remembers *sales process*. Different objective functions.

---

## 14. Tool Intelligence

Same pattern for tools:

- Usage frequency from `activities` / screen (deterministic)  
- Projects `built_with` / people `uses`  
- Dependency neighborhood  
- Replacement hypotheses (`hypothesized` only)  
- Timeline of adoption / abandonment  

**Planning payoff:** “Draft in Cursor” / “notify on Slack” grounded in *actual* tool graph, not generic agent defaults — feeds readiness and compilers without new proposal channels.

---

## 15. Graph health (continuous maintenance)

Worker jobs (calm, boot+hourly due pattern like economy):

| Job | Action | Gate |
|---|---|---|
| `kg_dedupe_suggest` | Merge candidates | Review UI |
| `kg_confidence_recal` | Recompute posteriors | Auto |
| `kg_temporal_close` | Propose valid_to on job-change | Review if trusted |
| `kg_gc_dormant` | new→dormant→archive suggest | Auto archive only thin+news-only |
| `kg_ontology_validate` | Illegal edges | Auto drop derived; review asserted |
| `kg_noise_scan` | News-only orgs, OCR junk | Align with people ambient fix |
| `kg_parity_diff` | Nightly v1↔v2 dual-write divergence report (shadow period only) — node/edge/read parity; persisted to `kg_parity_reports` + console badge. **Report-only, never auto-repairs** (I-2). Gates M3 cutover (§20). | Auto (report) |

**Split detection** (`split_candidate` clustering) is **deferred — post-PMF** (post-review Change 7); the manual "Split node" console action covers the recovery path (§4).

**Autonomy rule:** anything that changes user-visible truth about people/orgs they care about → review. Metadata/confidence/GC of thin nodes → auto.

---

## 16. Retrieval optimization

Replace “embed question → timeline” as primary with:

1. **Structured first** (v1 grounding already starts this): people, tasks, affiliations  
2. **Belief retrieval:** predicates with \(I \cdot conf \cdot recency\) in Now-Context neighborhood  
3. **Temporal filter (post-review Change 6):** for people/network query classes (person_lookup, org_people, network), affiliations retrieve `status IN ('active','superseded')` by default — "who do I know at Figma" implicitly includes people who *used* to work there; current ranks above former, and former is annotated with its interval ("Sarah (at Figma 2022–2024, now at Linear)"). Non-people predicates (`uses`, `depends_on`, org attrs) keep strict-current default. Explicit time in the question always wins over both defaults. Superseded citations carry interval + supersession pointer via `GET /kg/explain`.  
4. **Provenance-aware truncation:** prefer high-\(w\) evidence quotes in context pack  
5. **Semantic fallback last** (existing)

People-list fix (contacts roster) is the template: **intent → right index**, not one pile.

---

## 17. Explainability layer

Every affiliation answer ships:

```
Patrick works at Dell
Confidence 98% · current · last confirmed yesterday
Why we believe this:
  1. Email signature (2d ago) — weight 3.0
  2. Meeting transcript (5d ago) — “Patrick from Dell…”
  3. Calendar invite organizer domain dell.com
  4. You confirmed (console) — locked
```

API: `GET /kg/predicates/{id}/explain` → posterior breakdown + evidence list. Constellation evidence popover and chat sources both call it.

---

## 18. Scalability (10M observations / 500k nodes / 50M edges / 20y)

| Concern | Design |
|---|---|
| Storage | SQLite remains system of record for beliefs+evidence; Lance for embeddings + event vectors; optional edge partition by `subj_id % N` later |
| Hot path | Mentions → claim queue → async resolve (already worker-shaped) |
| Indexes | `(subj, pred, status)`, temporal, evidence by predicate, alias FTS, domain |
| Rebuild | Stop full derived wipe of asserted world; derived materializations become *tables* refreshed incrementally |
| Streaming | Event bus → claim intake; nightly batch for importance/GC |
| Memory | Constellation never loads full graph — always top-I candidates + WM (Field invariant) |
| Query | Prefer SQL belief queries over graph walks >2 hops; precompute Org/Tool briefs |

**Failure mode:** giant `associated_with` clique — mitigated by banning derived social edges from constellation and capping derived degree.

---

## 19. UI implications

| Surface | v2 experience |
|---|---|
| Organization page | Living brief + people timeline + evidence | 
| Tool page | Usage + projects + deps + “last used” |
| Constellation | High-I / high-conf / activated nodes; evidence bags |
| People | Temporal affiliations (“Dell 2022–”, “Acme now”) |
| Chat | Intent-routed retrieval; explainable citations |
| Memory Console | Identity merges, adjudication, GC suggestions, ontology health |
| Graph Explorer (advanced) | Full predicate browser — power users only |
| Timeline | Belief changes as first-class events |

---

## 20. Migration strategy (from nexus_v1)

**Phase M0 — dual write (2–4 weeks)**  
Introduce `kg_nodes` / `kg_predicates` / `kg_evidence` beside `entities` / `relations`. Dual-write from `_persist_entities` and `add_relation`. Read path still v1.

**Phase M1 — backfill**  
- Every `entities`/`people` row → `kg_nodes`  
- Every `relations` row → `kg_predicates` + one `kg_evidence` from `source_event_id` if present  
- Set `layer` from `origin` (`user`→user_verified, `asserted`→asserted, `derived`→derived)  
- Confidence from existing `confidence` or defaults by layer  
- `valid_from = created_at`, `valid_to = NULL`

**Phase M2 — resolver cutover**  
New resolve path; leave_open queue shared with People v2.

**Phase M3 — read cutover**  
`context_for_person`, constellation, grounding read `kg_*`. Keep v1 tables as shadow for one release.

**Cutover gate (post-review Change 8):** M0–M3 run with the nightly `kg_parity_diff` job active (node parity, edge parity both directions, read parity on top-importance + protected nodes). M3 requires **7 consecutive nightly reports with zero critical deltas** (unmapped protected/trusted nodes, edge deltas on user-origin rows). The gate is encoded as a check (`kg_parity.cutover_ready()` / `GET /kg/parity`), not a convention.

**Phase M4 — retire thin writes**  
Stop mutating `relations` for new beliefs; retain as compatibility view.

**No information loss:** soft merges only; evidence always preserved; derived edges can be dropped and recomputed.

---

## 21. Testing strategy

| Layer | Tests |
|---|---|
| Normalize / block | Unit: OpenAI variants, Apple ambiguity |
| Score / decide | Golden pairs: must-merge / must-not-merge |
| Temporal | Job-change adjudication fixtures |
| Provenance | Multi-evidence explain snapshot |
| Policy | `knowledge_entities` enforced (fix the current gap) |
| Migration | Round-trip v1 → v2 → explain parity |
| Scale soak | Synthetic 1e5 nodes / 1e6 edges — constellation latency budget |
| Behavioral | Extend vinceo interface contracts: pins, trust gate, no derived pollution in focus |

---

## 22. Phased delivery (recommended)

| Phase | Scope | Exit |
|---|---|---|
| **KG-A** | ✅ **Shipped (Jul 22, 2026).** Evidence bags + predicate dual-write + enforce `knowledge_entities`; `GET /kg/explain` | Explainable affiliations; news cannot mint orgs lightly |
| **KG-B** | Temporal fields + supersession UX | “Where did X work in 2024?” |
| **KG-C** | Production resolver + merge review (automated split detection **deferred post-PMF**; manual split shipped) | OpenAI-class name fanout controlled |
| **KG-D** | Org + Tool Intelligence pages | Living briefs |
| **KG-E** | Importance engine wired to Field/retrieval | Better constellation without more nodes |
| **KG-F** | Ontology expansion modules | Capital / habits as optional packs |

Do **not** expand ontology before KG-A/B/C. Rich types on a thin trust substrate amplify noise.

---

## 23. Decision summary

| Decision | Choice | Why |
|---|---|---|
| Storage | Evolve SQLite belief store + Lance vectors | Local-first, fits existing worker |
| People vs entities | Unified `kg_nodes`, product split in UI | One resolver, two surfaces |
| Edge uniqueness | Drop unique (s,p,o); temporal versions | History of belief |
| Derived edges | Features only, not constellation truth | Stop pollution |
| Confidence | Evidence-log odds with source weights | Explainable, accumulates |
| External KG | Optional later | Privacy |
| CRM features | Explicitly out of scope | Wrong objective |

---

## 24. What this enables in 20 years

A user asks: *“What did we use before Cursor, and who at Dell still cares about that migration?”*

Mnemos answers from **temporal tool beliefs**, **org neighborhood**, **evidence-backed people**, and **importance**, with citations — not from a lucky embedding hit on a 2027 OCR fragment.

That is the bar for a Personal Cognitive OS memory substrate.

---

*Companion docs: `memory_field_cognitive_architecture.md` (attention), `cognitive_os_v2_roadmap.md` (sequencing), People v2 (`people_pipeline.py`, `source_policy.py`). This document owns Layers “semantic LTM / entity intelligence” only; it does not reopen approval gating (I-4) or blackboard timing.*
