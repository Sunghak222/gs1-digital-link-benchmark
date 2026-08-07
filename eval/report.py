"""run 폴더 → 자립형 리포트(report.html) 생성.

    python eval/report.py results/<run_dir>

1층: 대시보드(판정·4분면·실패 유형·슬라이스 정확도·시간 분포).
2층: 문항 상세 — 증거 사슬(후보 취급 → 읽은 페이지 → 추출본 → 값 도달) + 검수 입력(○/✗·메모,
브라우저 localStorage 저장, JSON 내보내기). grade.py 실행 후 사용.
UI는 한/영 전환 가능(헤더 토글) — 질문·답변 같은 데이터는 원문 그대로, 라벨과 판정 문구만 번역.
"""
from __future__ import annotations

import html
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import adapter  # noqa: E402
from analyze_failures import classify, gold_page_treatment, value_tokens  # noqa: E402

BENCH_ROOT = adapter.BENCH_ROOT


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def tail(p: str, n: int = 2) -> str:
    return "/".join(str(p).split("/")[-n:])


def extract_snippets(raw: dict[str, Any], gold_tails: set[str], tokens: list[str]) -> list[dict[str, Any]]:
    """정답 페이지의 '파이프라인이 받은 추출본' 발췌 + 값 포함 여부."""
    out = []
    for t in (raw.get("digital_link_result") or {}).get("traversed_links") or []:
        path = str((t.get("arguments") or {}).get("path") or "")
        if not any(g in path for g in gold_tails):
            continue
        d = t.get("data")
        text = d if isinstance(d, str) else json.dumps(d, ensure_ascii=False)
        out.append({
            "page": tail(path),
            "kind": d.get("extraction", "raw") if isinstance(d, dict) else "raw",
            "has_value": any(tok in text for tok in tokens),
            "text": text[:700],
        })
    return out


def build_items(run_dir: Path) -> list[dict[str, Any]]:
    results = {r["qa_id"]: r for r in load_jsonl(run_dir / "results.jsonl")}
    grades = load_jsonl(run_dir / "grades.jsonl")
    qa_by_id = {q["qa_id"]: q for q in load_jsonl(BENCH_ROOT / "qa.jsonl")}
    facts = {f["fact_id"]: f for f in load_jsonl(BENCH_ROOT / "facts.jsonl")}

    items = []
    for g in grades:
        qa, r = qa_by_id[g["qa_id"]], results[g["qa_id"]]
        raw = json.loads((run_dir / "raw" / f"{g['qa_id']}.json").read_text(encoding="utf-8"))
        tokens = [t for fid in qa["gold_fact_ids"] for t in value_tokens(facts.get(fid, {}).get("value"))]
        evidence = any(t in json.dumps(raw, ensure_ascii=False) for t in tokens) if tokens else False
        treatment = gold_page_treatment(raw, g.get("gold_pages") or [])
        gold_tails = {tail(p) for p in g.get("gold_pages") or []}
        category = ("정상" if g["verdict"] == "correct"
                    else classify(g, evidence, treatment, str(g.get("answer"))))
        cands = [{"url": tail(str(c.get("source_url", "")), 2), "action": c.get("action")}
                 for c in (raw.get("candidate_plan") or {}).get("candidates") or []]
        items.append({
            "qa_id": g["qa_id"], "entity": qa["entity"],
            "question": qa["question"], "gold": qa["gold_answer"],
            "gold_values": {fid: facts.get(fid, {}).get("value") for fid in qa["gold_fact_ids"]},
            "answer": str(g.get("answer")), "verdict": g["verdict"], "reason": g.get("judge_reason", ""),
            "quadrant": g.get("quadrant"), "category": category,
            "lang": qa["tags"]["lang"], "modality": qa["tags"]["modality"],
            "difficulty": qa["tags"].get("difficulty"),
            "page_hit": g.get("answer_page_hit"), "evidence": evidence, "treatment": treatment,
            "gold_pages": sorted(gold_tails), "read_pages": [tail(p) for p in r["retrieval"]["read_pages"]],
            "candidates": cands,
            "total_s": r["latency"]["total_s"], "nodes": r["latency"]["nodes"],
            "tokens": r["tokens"], "snippets": extract_snippets(raw, gold_tails, tokens),
        })
    return items


HTML_TEMPLATE = """<!doctype html>
<html lang="ko"><head><meta charset="utf-8">
<title>Eval report — __RUN__</title>
<style>
 body{font-family:-apple-system,'Apple SD Gothic Neo',sans-serif;margin:0;background:#f5f6f8;color:#1c1e21}
 header{background:#1f2937;color:#fff;padding:14px 24px;display:flex;justify-content:space-between;align-items:center}
 header h1{font-size:17px;margin:0} header .sub{color:#9ca3af;font-size:12px;margin-top:4px}
 #langBtn{background:#374151;color:#fff;border:1px solid #4b5563;border-radius:14px;padding:5px 14px;cursor:pointer;font-size:12.5px}
 .wrap{max-width:1200px;margin:0 auto;padding:16px 24px}
 .cards{display:flex;gap:10px;flex-wrap:wrap;margin:10px 0}
 .card{background:#fff;border:1px solid #e5e7eb;border-radius:10px;padding:12px 16px;min-width:120px}
 .card b{font-size:22px;display:block} .card span{font-size:12px;color:#6b7280}
 h2{font-size:15px;margin:22px 0 8px}
 .quad{display:grid;grid-template-columns:1fr 1fr;gap:8px;max-width:560px}
 .quad div{border-radius:10px;padding:10px 14px;cursor:pointer;border:1px solid #e5e7eb;background:#fff}
 .quad b{font-size:20px} .quad small{color:#6b7280;display:block}
 .bar{display:flex;align-items:center;gap:8px;margin:4px 0;font-size:13px;cursor:pointer}
 .bar i{height:14px;background:#6366f1;border-radius:3px;display:inline-block}
 table.slice{border-collapse:collapse;font-size:13px;background:#fff;border:1px solid #e5e7eb;border-radius:8px}
 table.slice td,table.slice th{padding:5px 12px;border-bottom:1px solid #f0f0f2;text-align:left}
 .note{font-size:12px;color:#92400e;background:#fef3c7;border-radius:8px;padding:8px 12px;margin-top:8px}
 .filters{display:flex;gap:8px;flex-wrap:wrap;margin:10px 0}
 select,input[type=text]{padding:6px 8px;border:1px solid #d1d5db;border-radius:6px;font-size:13px}
 .item{background:#fff;border:1px solid #e5e7eb;border-radius:10px;margin:8px 0;padding:12px 16px}
 .item .head{display:flex;justify-content:space-between;gap:8px;cursor:pointer;align-items:baseline}
 .item .q{font-weight:600;font-size:14px}
 .tag{display:inline-block;padding:1px 8px;border-radius:10px;font-size:11.5px;margin-left:4px}
 .v-correct{background:#dcfce7;color:#166534}.v-partial{background:#fef9c3;color:#854d0e}
 .v-no_answer{background:#e0e7ff;color:#3730a3}.v-incorrect{background:#fee2e2;color:#991b1b}
 .detail{display:none;border-top:1px dashed #e5e7eb;margin-top:10px;padding-top:10px;font-size:13.5px}
 .detail dt{font-weight:600;margin-top:8px} .detail dd{margin:2px 0 0 0}
 .chain{display:flex;gap:6px;flex-wrap:wrap;margin:6px 0}
 .step{border-radius:8px;padding:6px 10px;font-size:12.5px;border:1px solid #e5e7eb;background:#fafafa}
 .ok{border-color:#86efac;background:#f0fdf4}.bad{border-color:#fca5a5;background:#fef2f2}
 pre{background:#111827;color:#e5e7eb;padding:10px;border-radius:8px;font-size:12px;white-space:pre-wrap;max-height:260px;overflow:auto}
 mark{background:#fde047}
 .review{margin-top:10px;padding:8px;background:#f9fafb;border-radius:8px}
 .review button{margin-right:6px;padding:4px 12px;border-radius:6px;border:1px solid #d1d5db;background:#fff;cursor:pointer}
 .review button.sel-o{background:#dcfce7;border-color:#16a34a}.review button.sel-x{background:#fee2e2;border-color:#dc2626}
 .review textarea{width:100%;box-sizing:border-box;margin-top:6px;border:1px solid #d1d5db;border-radius:6px;font-size:13px;padding:6px}
 #exportBtn{position:fixed;right:20px;bottom:20px;background:#1f2937;color:#fff;border:0;border-radius:20px;padding:10px 18px;cursor:pointer}
 .hist{display:flex;align-items:flex-end;gap:2px;height:80px;margin:8px 0}
 .hist div{background:#93c5fd;width:22px;position:relative}
 .hist div span{position:absolute;bottom:-18px;font-size:10px;left:0;color:#6b7280}
</style></head><body>
<header>
 <div><h1 id="hTitle"></h1><div class="sub">__SUB__</div></div>
 <button id="langBtn"></button>
</header>
<div class="wrap">
 <div style="display:flex;gap:24px;align-items:center;flex-wrap:wrap">
  <div class="cards" id="cards" style="flex:1"></div>
  <div style="display:flex;align-items:center;gap:14px;background:#fff;border:1px solid #e5e7eb;border-radius:10px;padding:10px 16px">
   <svg id="donut" width="120" height="120" viewBox="0 0 120 120"></svg><div id="donutLegend" style="font-size:12.5px"></div>
  </div>
 </div>
 <h2 id="hQuad"></h2>
 <div class="quad" id="quad"></div>
 <h2 id="hCats"></h2><div id="cats"></div>
 <h2 id="hSlices"></h2><div id="slices" style="display:flex;gap:14px;flex-wrap:wrap"></div>
 <div class="note" id="sliceNote"></div>
 <h2 id="hHist"></h2><div class="hist" id="hist"></div>
 <h2 id="hList" style="margin-top:34px"></h2>
 <div class="filters">
  <select id="fVerdict"></select>
  <select id="fQuad"></select>
  <select id="fCat"></select>
  <select id="fDiff"></select>
  <select id="fMod"></select>
  <select id="fLang"></select>
  <input type="text" id="fText">
 </div>
 <div id="list"></div>
</div>
<button id="exportBtn"></button>
<script>
const RUN = "__RUN__";
const DATA = __DATA__;
const $ = s => document.querySelector(s);
const esc = s => String(s??"").replace(/[&<>]/g, c=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));
DATA.forEach(d=>d.kind = d.entity.startsWith("01/")?"food":"place");

// ---------- i18n ----------
const STR = {
 ko: {
  title:"평가 리포트", langBtn:"EN", items:"문항", acc:"정확도 (correct)", pni:"partial / 무응답 / 오답",
  runtime:"실행 누적", inputTok:"입력 토큰", min:"분",
  hQuad:"4분면 — 정답 페이지를 읽었나 × 답이 맞았나 (클릭해서 걸러보기)",
  hCats:"실패 유형 (correct 제외)", hSlices:"슬라이스별 정확도", hHist:"문항당 시간 분포 (초)",
  hList:"문항 목록",
  sliceNote:"⚠ 해석 주의: image는 파이프라인이 사진 대신 HTML을 읽고 답한 결과가 섞여 있어 이미지 읽기 능력의 점수가 아님 (증거 제한 모드 필요). hard는 멀티홉 문항이 이 run에 없으면 4문항뿐이라 통계적 의미가 약함 (멀티홉은 hit 실험에서 실행).",
  fVerdict:"판정: 전체", fQuad:"4분면: 전체", fCat:"실패 유형: 전체", fDiff:"난이도: 전체",
  fMod:"modality: 전체", fLang:"질문 언어: 전체", fText:"질문/답변/qa_id 검색",
  sLang:"언어", sMod:"modality", sDiff:"난이도", sKind:"엔티티 종류", sAcc:"정확도",
  food:"음식", place:"장소",
  gold:"정답", answer:"시스템 답변", judge:"채점 근거", chainT:"증거 사슬 — 실패 유형", read:"읽은 페이지",
  cands:"후보 전체", nodes:"노드별 시간", trace:"원본 추적", none:"(없음)",
  extractOf:"파이프라인이 받은 추출본", hasVal:"값 포함", noVal:"값 없음", raw:"원문 그대로",
  c1:"① 후보 선택: 정답 페이지", c1t:"취급", c2:"② 정답 페이지 읽음", c3:"③ 정답 값 프롬프트 도달", c4:"④ 판정",
  yes:"예", no:"아니오", na:"대조불가",
  review:"검수:", ok:"○ 판정 맞음", wrong:"✗ 판정 틀림", memo:"메모", reviewed:"검수",
  exportBtn:"검수 내보내기 (JSON)", count:"문항",
  quad:{"읽음·맞음(정상)":"읽음·맞음 (정상)","읽음·틀림(읽기/합성 실패)":"읽음·틀림 (읽기/합성 실패)","못읽음·틀림(검색 실패)":"못읽음·틀림 (검색 실패)","못읽음·맞음(요주의: 사전지식 의심)":"못읽음·맞음 (요주의: 사전지식 의심)","대조불가":"대조불가"},
  cat:{"정상":"정상","A":"A. 합성 실패 — 증거가 프롬프트에 도달했는데 놓침","B":"B. 추출 유실 — 페이지는 읽었지만 값이 추출에서 빠짐","C":"C. 후보 선택이 정답 페이지를 건너뜀","D":"D. 검색/도달 실패 — 정답 페이지 미읽음","J":"J. 채점 규칙 이슈 의심 — 답변에 정답 값이 전부 있음"},
  treat:{"후보목록에 없음":"후보목록에 없음","traverse":"바로 읽기","preprocess":"인덱싱행","skip":"건너뜀"},
 },
 en: {
  title:"Evaluation report", langBtn:"한국어", items:"questions", acc:"accuracy (correct)", pni:"partial / no answer / wrong",
  runtime:"total runtime", inputTok:"input tokens", min:"min",
  hQuad:"Four quadrants — answer page read × answer correct (click to filter)",
  hCats:"Failure types (excluding correct)", hSlices:"Accuracy by slice", hHist:"Time per question (sec)",
  hList:"Questions",
  sliceNote:"⚠ Caution: 'image' scores include answers derived from HTML instead of the photo, so this is not an image-reading score (needs evidence-restricted mode). 'hard' has only 4 items when multi-hop questions are absent from this run (multi-hop runs in the KG-hit experiment).",
  fVerdict:"verdict: all", fQuad:"quadrant: all", fCat:"failure type: all", fDiff:"difficulty: all",
  fMod:"modality: all", fLang:"question language: all", fText:"search question/answer/qa_id",
  sLang:"language", sMod:"modality", sDiff:"difficulty", sKind:"entity type", sAcc:"accuracy",
  food:"food", place:"place",
  gold:"Expected answer", answer:"System answer", judge:"Judge reason", chainT:"Evidence chain — failure type", read:"Pages read",
  cands:"All candidates", nodes:"Per-node time", trace:"Raw trace", none:"(none)",
  extractOf:"Extraction the pipeline received for", hasVal:"contains value", noVal:"value missing", raw:"raw",
  c1:"① Candidate selection: answer page", c1t:"treated as", c2:"② Answer page read", c3:"③ Expected value reached prompt", c4:"④ Verdict",
  yes:"yes", no:"no", na:"n/a",
  review:"Review:", ok:"○ verdict correct", wrong:"✗ verdict wrong", memo:"note", reviewed:"reviewed",
  exportBtn:"Export review (JSON)", count:"questions",
  quad:{"읽음·맞음(정상)":"Read · Correct (OK)","읽음·틀림(읽기/합성 실패)":"Read · Wrong (reading/synthesis failure)","못읽음·틀림(검색 실패)":"Not read · Wrong (retrieval failure)","못읽음·맞음(요주의: 사전지식 의심)":"Not read · Correct (suspicious: prior knowledge)","대조불가":"n/a"},
  cat:{"정상":"OK","A":"A. Synthesis failure — evidence reached the prompt but was missed","B":"B. Extraction loss — page was read but the value was dropped during extraction","C":"C. Candidate selector skipped the answer page","D":"D. Retrieval failure — answer page never read","J":"J. Suspected grading-rule issue — answer contains all expected values"},
  treat:{"후보목록에 없음":"not in candidate list","traverse":"read directly","preprocess":"sent to indexing","skip":"skipped"},
 }
};
let LANG = localStorage.getItem("reportLang") || "ko";
const T = k => STR[LANG][k] ?? STR.ko[k] ?? k;
const tQuad = q => (STR[LANG].quad[q] ?? q);
const tCat = c => c==="정상" ? STR[LANG].cat["정상"] : (STR[LANG].cat[c?.[0]] ?? c);
const tTreat = x => String(x).split("/").map(a=>STR[LANG].treat[a] ?? a).join("/");

const state = {verdict:"", quad:"", cat:"", diff:"", mod:"", lang:"", text:""};
function counts(key){ const c={}; DATA.forEach(d=>{const k=d[key]??"?"; c[k]=(c[k]||0)+1}); return c; }
function accStr(list){ const n=list.length; const ok=list.filter(d=>d.verdict==="correct").length; return n? (100*ok/n).toFixed(1)+"% ("+ok+"/"+n+")" : "-"; }
const v = counts("verdict");

function renderTop(){
 $("#hTitle").textContent = T("title")+" — "+RUN;
 $("#langBtn").textContent = T("langBtn");
 ["hQuad","hCats","hSlices","hHist","hList"].forEach(id=>$("#"+id).textContent=T(id));
 $("#sliceNote").textContent = T("sliceNote");
 $("#exportBtn").textContent = T("exportBtn");
 $("#fText").placeholder = T("fText");
 $("#cards").innerHTML = `
  <div class="card"><b>${DATA.length}</b><span>${T("items")}</span></div>
  <div class="card"><b>${accStr(DATA).split(" ")[0]}</b><span>${T("acc")}</span></div>
  <div class="card"><b>${v.partial||0} / ${v.no_answer||0} / ${v.incorrect||0}</b><span>${T("pni")}</span></div>
  <div class="card"><b>${(DATA.reduce((a,d)=>a+d.total_s,0)/60).toFixed(0)}${T("min")}</b><span>${T("runtime")}</span></div>
  <div class="card"><b>${(DATA.reduce((a,d)=>a+(d.tokens.input||0),0)/1e6).toFixed(2)}M</b><span>${T("inputTok")}</span></div>`;

 const order=["correct","partial","no_answer","incorrect"];
 const colors={correct:"#22c55e",partial:"#eab308",no_answer:"#6366f1",incorrect:"#ef4444"};
 const total=DATA.length; let acc=0, paths="";
 order.filter(k=>v[k]).forEach(k=>{
  const frac=v[k]/total, a0=acc*2*Math.PI, a1=(acc+frac)*2*Math.PI; acc+=frac;
  if(frac>=0.999){ paths+=`<circle cx="60" cy="60" r="46" stroke="${colors[k]}" stroke-width="22" fill="none"/>`; return; }
  const x0=60+46*Math.sin(a0), y0=60-46*Math.cos(a0), x1=60+46*Math.sin(a1), y1=60-46*Math.cos(a1);
  paths+=`<path d="M ${x0} ${y0} A 46 46 0 ${frac>0.5?1:0} 1 ${x1} ${y1}" stroke="${colors[k]}" stroke-width="22" fill="none"/>`;
 });
 $("#donut").innerHTML = paths + `<text x="60" y="66" text-anchor="middle" font-size="18" font-weight="700">${(100*(v.correct||0)/total).toFixed(0)}%</text>`;
 $("#donutLegend").innerHTML = order.filter(k=>v[k]).map(k=>
  `<div><span style="display:inline-block;width:10px;height:10px;border-radius:2px;background:${colors[k]};margin-right:6px"></span>${k} ${v[k]} (${(100*v[k]/total).toFixed(1)}%)</div>`).join("");

 const quads = counts("quadrant");
 $("#quad").innerHTML = Object.entries(quads).map(([k,n])=>
  `<div onclick="setF('quad','${k.replace(/'/g,"\\'")}')"><b>${n}</b><small>${esc(tQuad(k))}</small></div>`).join("");

 const cats = {}; DATA.filter(d=>d.verdict!=="correct").forEach(d=>cats[d.category]=(cats[d.category]||0)+1);
 const maxc = Math.max(...Object.values(cats),1);
 $("#cats").innerHTML = Object.entries(cats).sort((a,b)=>b[1]-a[1]).map(([k,n])=>
  `<div class="bar" onclick="setF('cat','${k.replace(/'/g,"\\'")}')"><i style="width:${240*n/maxc}px"></i><b>${n}</b> ${esc(tCat(k))}</div>`).join("");

 function sliceTable(title,key,map){
  const groups={}; DATA.forEach(d=>{(groups[d[key]??"?"] ||= []).push(d)});
  return `<table class="slice"><tr><th>${title}</th><th>${T("sAcc")}</th></tr>`+
   Object.entries(groups).map(([k,l])=>`<tr><td>${esc(map?map(k):k)}</td><td>${accStr(l)}</td></tr>`).join("")+"</table>";
 }
 $("#slices").innerHTML = [sliceTable(T("sLang"),"lang"),sliceTable(T("sMod"),"modality"),
  sliceTable(T("sDiff"),"difficulty"),sliceTable(T("sKind"),"kind",k=>T(k))].join("");

 const buckets = Array(12).fill(0);
 DATA.forEach(d=>buckets[Math.min(11, Math.floor(d.total_s/2))]++);
 const maxb = Math.max(...buckets,1);
 $("#hist").innerHTML = buckets.map((n,i)=>`<div style="height:${76*n/maxb+2}px" title="${i*2}~${i*2+2}s: ${n}"><span>${i*2}</span></div>`).join("");

 // 필터 옵션 — [select id, DATA 키, state 키, 표시 변환]
 [["fVerdict","verdict","verdict",null],["fQuad","quadrant","quad",tQuad],["fCat","category","cat",tCat],
  ["fDiff","difficulty","diff",null],["fMod","modality","mod",null],["fLang","lang","lang",null]].forEach(([id,key,st,map])=>{
  const sel=$("#"+id); const cur=state[st]; sel.innerHTML="";
  const d=document.createElement("option"); d.value=""; d.textContent=T(id); sel.appendChild(d);
  Object.keys(counts(key)).forEach(k=>{const o=document.createElement("option");o.value=k;o.textContent=map?map(k):k;sel.appendChild(o)});
  sel.value=cur; sel.onchange=()=>{state[st]=sel.value; render()};
 });
}

function setF(k,v){ state[k]=v; renderTop(); render(); location.hash="#list"; }

const store = JSON.parse(localStorage.getItem("review:"+RUN)||"{}");
function saveReview(id, field, val){ (store[id] ||= {})[field]=val; localStorage.setItem("review:"+RUN, JSON.stringify(store)); render(); }
$("#exportBtn").onclick=()=>{
 const blob=new Blob([JSON.stringify({run:RUN,review:store},null,2)],{type:"application/json"});
 const a=document.createElement("a"); a.href=URL.createObjectURL(blob); a.download="review-"+RUN+".json"; a.click();
};
$("#langBtn").onclick=()=>{ LANG = LANG==="ko"?"en":"ko"; localStorage.setItem("reportLang",LANG); renderTop(); render(); };

function chainHTML(d){
 const golds = d.gold_pages.join(", ");
 const steps = [
  [`${T("c1")}(${esc(golds)}) ${T("c1t")} = ${esc(tTreat(d.treatment))}`, d.treatment!=="후보목록에 없음" && d.treatment!=="skip"],
  [`${T("c2")}: ${d.page_hit===null?T("na"):d.page_hit?T("yes"):T("no")}`, !!d.page_hit],
  [`${T("c3")}: ${d.evidence?T("yes"):T("no")}`, d.evidence],
  [`${T("c4")}: ${d.verdict}`, d.verdict==="correct"],
 ];
 return `<div class="chain">`+steps.map(([t,ok])=>`<span class="step ${ok?"ok":"bad"}">${t}</span>`).join("")+`</div>`;
}
function snippetsHTML(d){
 if(!d.snippets.length) return "";
 return d.snippets.map(s=>{
  let txt = esc(s.text);
  Object.values(d.gold_values).flat().forEach(v=>{ const t=esc(String(typeof v==="object"?"":v)); if(t && t.length>1) txt=txt.split(t).join("<mark>"+t+"</mark>"); });
  const kind = s.kind==="raw" ? T("raw") : s.kind;
  return `<dt>${T("extractOf")} ${esc(s.page)} (${esc(kind)}${s.has_value?", "+T("hasVal"):", <b>"+T("noVal")+"</b>"})</dt><dd><pre>${txt}</pre></dd>`;
 }).join("");
}
function render(){
 const list = DATA.filter(d=>
  (!state.verdict||d.verdict===state.verdict)&&(!state.quad||d.quadrant===state.quad)&&
  (!state.cat||d.category===state.cat)&&
  (!state.diff||d.difficulty===state.diff)&&(!state.mod||d.modality===state.mod)&&
  (!state.lang||d.lang===state.lang)&&
  (!state.text||(d.question+d.answer+d.qa_id).toLowerCase().includes(state.text)));
 $("#list").innerHTML = `<div style="font-size:13px;color:#6b7280">${list.length} ${T("count")}</div>`+list.map(d=>{
  const r = store[d.qa_id]||{};
  return `<div class="item">
   <div class="head" onclick="this.nextElementSibling.style.display=this.nextElementSibling.style.display==='block'?'none':'block'">
    <span class="q">${esc(d.question)}</span>
    <span><span class="tag v-${d.verdict}">${d.verdict}</span>${r.mark?`<span class="tag" style="background:#e5e7eb">${T("reviewed")} ${r.mark}</span>`:""}
    <span style="color:#9ca3af;font-size:12px">${d.total_s}s</span></span>
   </div>
   <div class="detail">
    <dl>
     <dt>${T("gold")}</dt><dd>${esc(d.gold)} <span style="color:#6b7280">(${esc(JSON.stringify(d.gold_values))})</span></dd>
     <dt>${T("answer")}</dt><dd>${esc(d.answer)}</dd>
     <dt>${T("judge")} (${esc(d.verdict)})</dt><dd>${esc(d.reason)}</dd>
     <dt>${T("chainT")}: ${esc(tCat(d.category))}</dt><dd>${chainHTML(d)}</dd>
     <dt>${T("read")}</dt><dd>${d.read_pages.map(esc).join(", ")||T("none")}</dd>
     <dt>${T("cands")}</dt><dd>${d.candidates.map(c=>`${esc(c.url)} <b>[${esc(tTreat(c.action))}]</b>`).join(" · ")||T("none")}</dd>
     ${snippetsHTML(d)}
     <dt>${T("nodes")}</dt><dd>${Object.entries(d.nodes).map(([k,s])=>`${esc(k)} ${s}s`).join(" · ")}</dd>
     <dt>${T("trace")}</dt><dd><code>raw/${d.qa_id}.json</code> · <code>entities/${d.entity.replace("/","-")}/pages/</code></dd>
    </dl>
    <div class="review">${T("review")}
     <button class="${r.mark==="○"?"sel-o":""}" onclick="saveReview('${d.qa_id}','mark','○')">${T("ok")}</button>
     <button class="${r.mark==="✗"?"sel-x":""}" onclick="saveReview('${d.qa_id}','mark','✗')">${T("wrong")}</button>
     <textarea rows="1" placeholder="${T("memo")}" onchange="saveReview('${d.qa_id}','note',this.value)">${esc(r.note||"")}</textarea>
    </div>
   </div></div>`;
 }).join("");
}
renderTop(); render();
</script></body></html>
"""


def main() -> None:
    run_dir = Path(sys.argv[1])
    items = build_items(run_dir)
    run_info = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    sub = (f"{run_info.get('kg_state')}/{run_info.get('runner','')} · {run_info.get('corpus')} · "
           f"{run_info.get('qa_count')} QA · chat={run_info.get('models',{}).get('chat','?')} · "
           f"{run_info.get('started_at','')[:16]}")
    page = (HTML_TEMPLATE
            .replace("__RUN__", html.escape(run_dir.name))
            .replace("__SUB__", html.escape(sub))
            .replace("__DATA__", json.dumps(items, ensure_ascii=False)))
    out = run_dir / "report.html"
    out.write_text(page, encoding="utf-8")
    print(f"리포트 생성: {out}  ({out.stat().st_size/1e6:.1f} MB, {len(items)}문항)")


if __name__ == "__main__":
    main()
