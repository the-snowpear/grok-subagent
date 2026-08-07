const FALLBACK_WINDOW = 1_000_000;
const zero = () => ({ input:0, output:0, cacheRead:0, cacheWrite:0, reasoning:0, total:0 });
const fresh = (agentId="") => ({ agentId, lastSeq:0, calls:0, total:zero(), latest:zero(), usageTurns:new Set(), endTurns:new Set(), costTurns:new Set(), costUsd:0, context:null, model:"" });
let state=fresh(), stream=null, host=null, pop=null, panel=null;

const rec = (v) => v && typeof v === "object" && !Array.isArray(v) ? v : null;
function payload(e){ if(!e?.payload) return null; if(typeof e.payload === "object") return rec(e.payload); try{return rec(JSON.parse(e.payload));}catch{return null;} }
function num(r,...keys){ if(!r)return 0; for(const k of keys){const v=r[k]; const n=typeof v==="number"?v:Number(v); if(Number.isFinite(n))return Math.max(0,n);} return 0; }
function usage(r){
  if(!r)return null;
  const x={
    input:num(r,"input_tokens","inputTokens"), output:num(r,"output_tokens","outputTokens"),
    cacheRead:num(r,"cache_read_input_tokens","cached_read_tokens","cacheReadInputTokens","cachedReadTokens"),
    cacheWrite:num(r,"cache_creation_input_tokens","cache_creation_tokens","cacheCreationInputTokens","cacheCreationTokens"),
    reasoning:num(r,"reasoning_tokens","reasoningTokens"), total:num(r,"total_tokens","totalTokens")
  };
  if(!x.total)x.total=x.input+x.cacheRead+x.cacheWrite+x.output;
  return Object.values(x).some(Boolean)?x:null;
}
function eventUsage(e){const p=payload(e); if(!p)return null; return usage(rec(p.usage))||usage(rec(rec(p.data)?.usage))||usage(p);}
function add(a,b){return {input:a.input+b.input,output:a.output+b.output,cacheRead:a.cacheRead+b.cacheRead,cacheWrite:a.cacheWrite+b.cacheWrite,reasoning:a.reasoning+b.reasoning,total:a.total+b.total};}
const turnKey=(e)=>e.turn_id==null?`seq:${e.seq}`:`turn:${e.turn_id}`;

function contextSnapshot(e){
  const p=payload(e), c=rec(p?.context); if(!c)return null; const total=num(c,"total"); if(!total)return null;
  const raw=Array.isArray(c.usage_categories)?c.usage_categories:Array.isArray(c.usageCategories)?c.usageCategories:[];
  return {
    used:num(c,"used"), total, system:num(c,"system_prompt_tokens","systemPromptTokens"), messages:num(c,"message_tokens","messageTokens"),
    messageCount:num(c,"message_count","messageCount"), toolTokens:num(c,"tool_definitions_tokens","toolDefinitionsTokens"), toolCount:num(c,"tool_definitions_count","toolDefinitionsCount"),
    free:num(c,"free_tokens","freeTokens"), turns:num(c,"turn_count","turnCount"), toolCalls:num(c,"tool_call_count","toolCallCount"), compactions:num(c,"compaction_count","compactionCount"),
    threshold:num(c,"auto_compact_threshold_percent","autoCompactThresholdPercent")||85,
    categories:raw.map(rec).filter(Boolean).map(x=>({label:String(x.label||"Context category"),tokens:num(x,"tokens"),detail:String(x.detail||"")}))
  };
}

function consume(e){
  if(!e||typeof e.seq!=="number"||e.seq<=state.lastSeq)return; state.lastSeq=e.seq; const key=turnKey(e);
  if(e.type==="usage"){
    const u=eventUsage(e); if(!u)return; state.latest=u; state.total=add(state.total,u); state.calls++; state.usageTurns.add(key); draw(); return;
  }
  if(e.type==="end"){
    const p=payload(e);
    if(!state.endTurns.has(key)){state.endTurns.add(key); if(!state.usageTurns.has(key)){const u=eventUsage(e); if(u){state.latest=u; state.total=add(state.total,u); state.calls+=Math.max(1,num(p,"num_turns","numTurns"));}}}
    if(!state.costTurns.has(key)){state.costTurns.add(key); const bad=Boolean(p?.usage_is_incomplete||p?.usageIsIncomplete||p?.cost_is_partial||p?.costIsPartial); if(!bad){const ticks=num(p,"total_cost_usd_ticks","totalCostUsdTicks"), usd=num(p,"total_cost_usd","totalCostUsd"); state.costUsd+=ticks?ticks/1e10:usd;}}
    draw(); return;
  }
  if(e.type==="context_usage"){
    const c=contextSnapshot(e); if(!c)return; const p=payload(e); state.context=c; state.model=String(p?.model_display_name||p?.model||p?.resolved_model_id||""); draw();
  }
}

function agentId(){const m=location.hash.match(/agents\/([^/?#]+)/); return m?.[1]?decodeURIComponent(m[1]):"";}
function fallbackWindow(){try{const n=Number(localStorage.getItem("grok-observer-context-window")||""); return Number.isFinite(n)&&n>0?n:FALLBACK_WINDOW;}catch{return FALLBACK_WINDOW;}}
const prompt=(u)=>u.input+u.cacheRead+u.cacheWrite;
const hit=(u)=>prompt(u)?u.cacheRead/prompt(u)*100:0;
function contextNumbers(){if(state.context){const total=state.context.total,used=Math.min(state.context.used,total); return {used,total,pct:total?used/total*100:0,real:true};} const total=fallbackWindow(),used=Math.min(prompt(state.latest),total); return {used,total,pct:total?used/total*100:0,real:false};}
function compact(n){if(!Number.isFinite(n))return "—"; if(Math.abs(n)>=1e6)return `${(n/1e6).toFixed(Math.abs(n)>=1e7?1:2).replace(/\.0+$/,"")}M`; if(Math.abs(n)>=1e3)return `${(n/1e3).toFixed(Math.abs(n)>=1e5?0:1).replace(/\.0$/,"")}k`; return Math.round(n).toLocaleString();}
const full=(n)=>Math.round(n).toLocaleString("en-US");
function percent(n){if(!Number.isFinite(n))return "0%"; if(n>=99.95)return "100%"; return `${n.toFixed(n>=10?1:2)}%`;}
function money(n){if(!Number.isFinite(n)||n<=0)return "—"; return `$${n.toFixed(n<.01?4:2)}`;}
function esc(v){return String(v??"").replaceAll("&","&amp;").replaceAll("<","&lt;").replaceAll(">","&gt;").replaceAll('"',"&quot;").replaceAll("'","&#39;");}

function ensureHost(){if(host?.isConnected)return host; host=document.getElementById("observer-telemetry-host")||document.body.appendChild(document.createElement("div")); host.id="observer-telemetry-host"; host.className="observer-telemetry"; return host;}
const button=(kind,html,label)=>`<button class="telemetry-metric" data-panel="${kind}" aria-label="${esc(label)}">${html}</button>`;
function draw(){
  const h=ensureHost(); if(!state.agentId){h.hidden=true; close(); return;} h.hidden=false; const c=contextNumbers();
  h.innerHTML=[button("tokens",`<span>↑</span><strong>${compact(state.total.input)}</strong>`,"Token usage"),button("tokens",`<span>↓</span><strong>${compact(state.total.output)}</strong>`,"Token usage"),`<i></i>`,button("tokens",`<strong>${compact(state.total.cacheRead)}</strong><small>cache</small>`,"Cache usage"),`<i></i>`,button("context",`<b class="telemetry-ring" style="--p:${Math.min(100,c.pct)}"></b><strong>${compact(c.used)}/${compact(c.total)}</strong><small>${percent(c.pct)}</small>`,"Context usage")].join("");
  h.querySelectorAll("[data-panel]").forEach(el=>el.onclick=(ev)=>{ev.stopPropagation(); const p=el.dataset.panel; if(panel===p&&pop?.isConnected)close(); else open(p,el);}); if(panel&&pop?.isConnected)renderPanel(panel);
}
function open(kind,anchor){close(); panel=kind; pop=document.body.appendChild(document.createElement("section")); pop.className="telemetry-popover"; pop.setAttribute("role","dialog"); renderPanel(kind); position(anchor);}
function close(){pop?.remove();pop=null;panel=null;}
function position(anchor){if(!pop)return; const r=anchor.getBoundingClientRect(),w=Math.min(540,Math.max(340,innerWidth-20)); pop.style.width=`${w}px`; pop.style.left=`${Math.max(10,Math.min(innerWidth-w-10,r.right-w))}px`; pop.style.top=`${Math.min(innerHeight-40,r.bottom+8)}px`;}
const row=(name,value,hint="")=>`<div class="telemetry-row"><span>${esc(name)}${hint?`<small>${esc(hint)}</small>`:""}</span><strong>${typeof value==="number"?full(value):esc(value)}</strong></div>`;
function ctxRow(name,tokens,total,detail="",cls=""){return `<div class="telemetry-context-row ${cls}"><span class="telemetry-dot"></span><span>${esc(name)}${detail?`<small>${esc(detail)}</small>`:""}</span><strong>${compact(tokens)} <small>${percent(total?tokens/total*100:0)}</small></strong></div>`;}

function renderPanel(kind){
  if(!pop)return;
  if(kind==="tokens"){
    const processed=state.total.total||prompt(state.total)+state.total.output;
    pop.innerHTML=`<header><div><em>TOKEN USAGE</em><h2>Token 用量</h2></div><mark>${state.calls} calls</mark></header><div class="telemetry-total"><span>累计处理</span><strong>${full(processed)}</strong></div><div class="telemetry-cards"><div><span>↑ Input</span><strong>${compact(state.total.input)}</strong><small>未缓存输入</small></div><div><span>↓ Output</span><strong>${compact(state.total.output)}</strong><small>模型输出</small></div><div><span>Cache read</span><strong>${compact(state.total.cacheRead)}</strong><small>${percent(hit(state.total))} hit</small></div><div><span>Reasoning</span><strong>${compact(state.total.reasoning)}</strong><small>Output 子集</small></div><div><span>Cost</span><strong>${money(state.costUsd)}</strong><small>完整 server cost</small></div><div><span>Calls</span><strong>${full(state.calls)}</strong><small>model responses</small></div></div><h3>累计明细</h3><div class="telemetry-rows">${row("Uncached input",state.total.input)}${row("Cache read",state.total.cacheRead)}${row("Cache write",state.total.cacheWrite)}${row("Output",state.total.output)}${row("Reasoning",state.total.reasoning,"不重复计入 Total")}</div><h3>最近一次模型调用</h3><div class="telemetry-rows">${row("Prompt context",prompt(state.latest))}${row("Uncached input",state.latest.input)}${row("Cache read",state.latest.cacheRead)}${row("Cache write",state.latest.cacheWrite)}${row("Output",state.latest.output)}${row("Reasoning",state.latest.reasoning)}</div><footer>来源：Grok <code>streaming-json</code> 的逐 response <code>usage</code>；旧版本缺失时才用 <code>end.usage</code> 回退。Cache hit = cache read / prompt buckets。</footer>`; return;
  }
  renderContext();
}
function renderContext(){
  if(!pop)return; const n=contextNumbers();
  if(!state.context){
    const free=Math.max(0,n.total-n.used), a=state.latest.input/n.total*100, b=(state.latest.input+state.latest.cacheRead)/n.total*100, d=n.used/n.total*100;
    pop.innerHTML=`<header><div><em>CONTEXT</em><h2>Context 构成</h2></div><mark>${percent(n.pct)}</mark></header><div class="telemetry-total"><span>最近一次 Prompt（估算）</span><strong>${full(n.used)} / ${full(n.total)}</strong></div><div class="telemetry-bar" style="background:linear-gradient(90deg,var(--blue) 0% ${a}%,var(--accent) ${a}% ${b}%,var(--amber) ${b}% ${d}%,var(--line) ${d}% 100%)"></div><div class="telemetry-rows">${row("Uncached input",state.latest.input)}${row("Cache read",state.latest.cacheRead)}${row("Cache write",state.latest.cacheWrite)}${row("Free",free)}</div><aside><strong>等待 Grok ContextInfo</strong>当前先按最近一次 prompt buckets 估算。回合结束后会用只读 ACP <code>x.ai/session/info</code> 自动替换。累计 Token 不会被当成当前 Context。</aside>`; return;
  }
  const c=state.context,total=c.total,used=Math.min(c.used,total),free=c.free||Math.max(0,total-used),overhead=Math.max(0,used-c.system-c.messages),sys=c.system/total*100,msg=(c.system+c.messages)/total*100,ov=used/total*100,threshold=Math.floor(total*Math.min(100,c.threshold)/100),until=Math.max(0,threshold-used);
  const info=[ctxRow("Tool definitions",c.toolTokens,total,`${c.toolCount} tools`,"info"),...c.categories.map(x=>ctxRow(x.label,x.tokens,total,x.detail,"info"))].join("");
  pop.innerHTML=`<header><div><em>CONTEXT</em><h2>Context 构成</h2>${state.model?`<small>${esc(state.model)}</small>`:""}</div><mark>${percent(used/total*100)}</mark></header><div class="telemetry-total"><span>当前 Context</span><strong>${full(used)} / ${full(total)}</strong></div><div class="telemetry-bar" style="background:linear-gradient(90deg,var(--context-system) 0% ${sys}%,var(--context-messages) ${sys}% ${msg}%,var(--context-overhead) ${msg}% ${ov}%,var(--line) ${ov}% 100%)"></div><div class="telemetry-context-rows">${ctxRow("System prompt",c.system,total,"","system")}${ctxRow("Messages",c.messages,total,`${c.messageCount} items`,"messages")}${overhead?ctxRow("Reasoning / overhead",overhead,total,"","overhead"):""}${ctxRow("Free",free,total,"","free")}</div><h3>来源信息（与 Messages / Overhead 可能重叠）</h3><div class="telemetry-context-rows">${info}</div><div class="telemetry-stats"><div><span>Auto-compact</span><strong>${c.threshold}%</strong></div><div><span>距阈值</span><strong>~${compact(until)}</strong></div><div><span>Turns</span><strong>${full(c.turns)}</strong></div><div><span>Tool calls</span><strong>${full(c.toolCalls)}</strong></div><div><span>Compactions</span><strong>${full(c.compactions)}</strong></div></div><footer>来源：Grok ACP <code>x.ai/session/info</code>。Skills / MCP servers 是 Grok 的信息行，可能已包含在 Messages 中，因此不会再次计入总量。</footer>`;
}

async function history(id){let after=0; for(let page=0;page<50;page++){const r=await fetch(`/api/events?agent_id=${encodeURIComponent(id)}&after=${after}`); if(!r.ok)throw new Error(String(r.status)); const body=await r.json(),events=Array.isArray(body.events)?body.events:[]; events.forEach(consume); if(!events.length)break; after=events.at(-1)?.seq||after; if(events.length<1000)break;}}
function connect(id){stream?.close(); stream=new EventSource(`/api/stream?agent_id=${encodeURIComponent(id)}&after=${state.lastSeq}`); stream.onmessage=(m)=>{try{consume(JSON.parse(m.data));}catch{}};}
async function select(){const id=agentId(); if(id===state.agentId&&stream)return; stream?.close();stream=null;state=fresh(id);draw(); if(!id)return; try{await history(id);}catch{} connect(id);draw();}
function boot(){ensureHost(); addEventListener("hashchange",()=>void select()); addEventListener("resize",()=>{if(pop&&panel){const a=document.querySelector(`[data-panel="${panel}"]`);if(a)position(a);}}); document.addEventListener("click",e=>{if(pop?.contains(e.target)||host?.contains(e.target))return;close();}); void select();}
if(document.readyState==="loading")document.addEventListener("DOMContentLoaded",boot,{once:true}); else boot();
