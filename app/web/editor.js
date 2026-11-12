"use strict";

let editorChapter = null;
let editorOffset = 0;
const editorLimit = 20;
const unsavedSegments = new Set();
const editorWrites = new Set();

window.addEventListener("beforeunload", (event) => {
  if (unsavedSegments.size) { event.preventDefault(); event.returnValue = ""; }
});

async function waitForEdits() {
  await Promise.allSettled([...editorWrites]);
  if (unsavedSegments.size) throw new Error("ჯერ შეინახეთ ცვლილებები. შეცდომის შემთხვევაში თქვენი ტექსტი რედაქტორში რჩება.");
}

async function openEditor(chapterId) {
  await waitForEdits();
  editorChapter = chapterId;
  editorOffset = 0;
  $("flaggedOnly").checked = false;
  await loadEditor();
  $("editor").hidden = false;
  $("editor").scrollIntoView({ behavior:"smooth" });
}

async function refreshQuality() {
  if (!editorChapter) return;
  const report = await getJson("/api/chapters/" + editorChapter + "/quality");
  $("editorQuality").textContent = "დამტკიცებულია " + report.approved + " / " + report.segments +
    " · უთარგმნელი: " + report.missing_translations + " · კრიტიკული: " + report.open_critical +
    (report.ready_for_final_export ? " · მზადაა საბოლოო ექსპორტისთვის" : " · ხელმისაწვდომია სამუშაო ექსპორტი");
}

function editorCard(segment) {
  const card = el("article", { class:"segment-card" + (segment.flags.length ? " flagged" : "") });
  const badge = pill(STATUS[segment.status] || segment.status);
  const kinds = { heading:"სათაური", paragraph:"აბზაცი", table:"ცხრილის უჯრედი", figure:"ილუსტრაციის წარწერა",
    caption:"წარწერა", footnote:"სქოლიო", reference:"მითითება", list:"სია", list_item:"სიის პუნქტი",
    callout:"გამოყოფილი ტექსტი", equation:"ფორმულა", other:"ტექსტი" };
  card.appendChild(el("div", {}, [pill("გვ. " + (segment.page || "—") + " · " + (kinds[segment.kind] || "ტექსტი")), badge]));
  const input = el("textarea", { "aria-label":"ქართული თარგმანი", spellcheck:"false" });
  input.value = segment.translation || "";
  input.disabled = !segment.translatable;
  const source = el("div", {}, [el("h3", { text:"ინგლისური წყარო" }), el("pre", { text:segment.source })]);
  const target = el("div", {}, [el("h3", { text:"ქართული თარგმანი" }), input]);
  card.appendChild(el("div", { class:"segment-grid" }, [source, target]));
  const state = el("div", { class:"muted save-state", role:"status" });
  const flagList = el("div");
  const historyList = el("div");
  card.appendChild(state);
  card.appendChild(flagList);
  let versionId = segment.version_id;
  let savedText = input.value;
  let blocked = false;
  let debounce = null;
  let pending = Promise.resolve();

  function renderFlags(flags) {
    flagList.replaceChildren();
    flags.filter((flag) => !flag.resolved).forEach((flag) => {
      const line = el("div", {}, [pill(flag.severity, flag.severity === "critical" ? "bad" : "warn"),
        el("span", { text:flag.message })]);
      const resolve = el("button", { class:"ghost", text:"საკითხის გადაწყვეტა" });
      resolve.onclick = () => guarded(resolve, async () => {
        await queueSave();
        const note = window.prompt("რატომ იხურება ეს შენიშვნა? მიუთითეთ შესწორება ან დასაბუთება.");
        if (!note) return;
        await postJson("/api/qa-flags/" + flag.id + "/resolve", { expected_version_id:versionId, note });
        await updateFlags();
        await refreshQuality();
      });
      line.appendChild(resolve);
      flagList.appendChild(line);
    });
  }

  async function updateFlags() {
    const history = await getJson("/api/segments/" + segment.id + "/versions");
    renderFlags(history.flags);
  }

  async function persist() {
    clearTimeout(debounce);
    if (blocked) throw new Error("ვერსიების კონფლიქტია. თქვენი ტექსტი შენარჩუნებულია; დააკოპირეთ და გადაამოწმეთ ახალი ვერსია.");
    const text = input.value;
    if (text === savedText) { unsavedSegments.delete(segment.id); return; }
    if (!text.trim()) throw new Error("თარგმანი ცარიელი ვერ იქნება");
    state.textContent = "ინახება…";
    try {
      const result = await (await api("/api/segments/" + segment.id + "/translation", {
        method:"PUT", headers:{ "Content-Type":"application/json" },
        body:JSON.stringify({ text, expected_version_id:versionId })
      })).json();
      versionId = result.version_id;
      savedText = result.translation;
      badge.textContent = STATUS[result.status] || result.status;
      if (input.value === text) {
        input.value = savedText;
        unsavedSegments.delete(segment.id);
        state.textContent = "შენახულია";
      }
      await updateFlags();
      await refreshQuality();
    } catch (error) {
      if (error.message.startsWith("409:")) blocked = true;
      state.textContent = error.message + " — თქვენი ტექსტი რედაქტორში შენარჩუნებულია.";
      throw error;
    }
  }

  function queueSave() {
    const task = pending.catch(() => {}).then(persist);
    pending = task;
    editorWrites.add(task);
    task.then(() => editorWrites.delete(task), () => editorWrites.delete(task));
    return task;
  }

  input.oninput = () => {
    unsavedSegments.add(segment.id);
    state.textContent = "შეუნახავი ცვლილება";
    clearTimeout(debounce);
    debounce = setTimeout(() => queueSave().catch((error) => say(error.message, "error")), 900);
  };
  input.onblur = () => { if (unsavedSegments.has(segment.id)) queueSave().catch((error) => say(error.message, "error")); };
  if (segment.translatable) {
    const save = el("button", { class:"ghost", text:"შენახვა" });
    save.onclick = () => guarded(save, queueSave);
    const approve = el("button", { text:"თარგმანის დამტკიცება" });
    approve.onclick = () => guarded(approve, async () => {
      await queueSave();
      const result = await postJson("/api/segments/" + segment.id + "/approve", { expected_version_id:versionId });
      versionId = result.version_id;
      badge.textContent = STATUS[result.status];
      state.textContent = "დამტკიცებულია და დამატებულია თარგმანის მეხსიერებაში";
      await refreshQuality();
    });
    card.appendChild(save);
    card.appendChild(approve);
  }
  const history = el("button", { class:"ghost", text:"ვერსიების ისტორია" });
  history.onclick = () => guarded(history, async () => {
    const result = await getJson("/api/segments/" + segment.id + "/versions");
    historyList.replaceChildren(...result.versions.map((version) => el("details", {}, [
      el("summary", { text:version.stage + " · " + new Date(version.created_at).toLocaleString() +
        (version.current ? " · მიმდინარე" : "") }),
      el("p", { class:"muted", text:version.note || version.model || "" }),
      el("pre", { text:version.text })
    ])));
  });
  card.appendChild(history);
  card.appendChild(historyList);
  renderFlags(segment.flags);
  return card;
}

async function loadEditor() {
  await waitForEdits();
  const page = await getJson("/api/chapters/" + editorChapter + "/segments?limit=" + editorLimit +
    "&offset=" + editorOffset + "&flagged_only=" + $("flaggedOnly").checked);
  $("editorTitle").textContent = page.chapter.title;
  $("editorSegments").replaceChildren(...page.segments.map(editorCard));
  if (!page.segments.length) $("editorSegments").textContent = "ამ ფილტრით სეგმენტები არ არის.";
  $("editorPage").textContent = (page.total ? editorOffset + 1 : 0) + "–" +
    Math.min(editorOffset + editorLimit, page.total) + " / " + page.total;
  $("editorPrevious").disabled = editorOffset === 0;
  $("editorNext").disabled = editorOffset + editorLimit >= page.total;
  await refreshQuality();
}

$("editorPrevious").onclick = () => guarded(null, async () => { await waitForEdits(); editorOffset = Math.max(0, editorOffset - editorLimit); await loadEditor(); });
$("editorNext").onclick = () => guarded(null, async () => { await waitForEdits(); editorOffset += editorLimit; await loadEditor(); });
$("flaggedOnly").onchange = () => guarded(null, async () => { await waitForEdits(); editorOffset = 0; await loadEditor(); });
$("closeEditor").onclick = () => guarded(null, async () => { await waitForEdits(); $("editor").hidden = true; editorChapter = null; });

async function refreshControls() {
  const id = $("project").value;
  const [processing, runtime, jobs] = await Promise.all([
    getJson("/api/projects/" + id + "/processing"), getJson("/api/runtime"),
    getJson("/api/projects/" + id + "/jobs/details")]);
  $("jobDetails").replaceChildren(...jobs.map((job) => el("p", { class:"muted",
    text:job.kind + " · " + job.status + " · მცდელობა: " + job.attempts + (job.last_error ? " · " + job.last_error : "") })));
  const states = { running:"მუშაობს", paused:"შეჩერებულია", cancelled:"რიგი გაუქმებულია" };
  $("processing").textContent = states[processing.state] + " · მიმდინარე სამუშაოები: " + processing.running +
    (processing.state !== "running" && processing.running ? " — მიმდინარე მოთხოვნები დასრულდება; ახალი აღარ დაიწყება." : "");
  $("pause").disabled = processing.state !== "running";
  $("resume").disabled = processing.state === "running";
  $("pipeline").textContent = "AI ეტაპები: თარგმნა → რევიზია → " +
    (runtime.models.refine ? "ქართული რაფინირება" : "ქართული რაფინირება არ არის გამართული") + " → " +
    (runtime.models.qa ? "QA" : "AI QA არ არის გამართული; წესებით შემოწმება მუშაობს");
}

for (const action of ["pause", "resume", "cancel"]) {
  $(action).onclick = () => guarded($(action), async () => {
    if (action === "cancel" && !window.confirm("გაუქმდეს რიგში მყოფი სამუშაოები? შესრულებული შედეგები შენარჩუნდება. გაგრძელებით რიგის აღდგენა შეიძლება.")) return;
    await postJson("/api/projects/" + $("project").value + "/processing/" + action);
    await refresh();
  });
}

$("updateBudget").onclick = () => guarded($("updateBudget"), async () => {
  if ($("projectBudget").value === "") throw new Error("შეიყვანეთ ბიუჯეტი");
  await api("/api/projects/" + $("project").value, { method:"PATCH", headers:{ "Content-Type":"application/json" },
    body:JSON.stringify({ budget_usd:Number($("projectBudget").value) }) });
  await refresh();
});

async function searchTerms() {
  const terms = await getJson("/api/projects/" + $("project").value + "/glossary?limit=50&q=" + encodeURIComponent($("termSearch").value));
  $("termResults").replaceChildren(...terms.map((term) => {
    const line = el("div", { class:"muted", text:term.source_term + " → " + term.target_term });
    const edit = el("button", { class:"ghost", text:"შესწორება" });
    edit.onclick = () => { $("termEnglish").value = term.source_term; $("termGeorgian").value = term.target_term; };
    line.appendChild(edit);
    return line;
  }));
}

$("termSearch").oninput = () => guarded(null, searchTerms);
$("saveTerm").onclick = () => guarded($("saveTerm"), async () => {
  const source = $("termEnglish").value.trim(), target = $("termGeorgian").value.trim();
  if (!source || !target) throw new Error("შეავსეთ ორივე ტერმინი");
  await postJson("/api/projects/" + $("project").value + "/glossary", { source_term:source, target_term:target });
  await searchTerms();
  say("ტერმინი შენახულია");
});
$("exportGlossary").onclick = () => guarded($("exportGlossary"), () => download("/api/projects/" + $("project").value + "/glossary/export", "glossary.csv"));
$("importTM").onclick = () => guarded($("importTM"), async () => {
  const file = $("tmFile").files[0];
  if (!file) throw new Error("აირჩიეთ თარგმანის მეხსიერების ფაილი");
  const form = new FormData();
  form.append("file", file);
  const result = await (await api("/api/projects/" + $("project").value + "/tm/import", { method:"POST", body:form })).json();
  say("შენახულია " + result.stored + " ნათარგმნი წყვილი");
  $("tmFile").value = "";
});
