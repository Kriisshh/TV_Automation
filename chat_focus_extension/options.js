const $ = (id) => document.getElementById(id);

function getMode() {
  const r = document.querySelector('input[name="mode"]:checked');
  return r ? r.value : "fixed";
}
function setMode(m) {
  document.querySelectorAll('input[name="mode"]').forEach((x) => { x.checked = (x.value === m); });
}

chrome.storage.local.get(
  { checkMode: null, fixedMin: null, randMin: 2, randMax: 5, retrySecs: 30, checkMinutes: null },
  (r) => {
    const mode = r.checkMode || "fixed";
    const fixed = (r.fixedMin != null ? r.fixedMin : (r.checkMinutes || 2));
    setMode(mode);
    $("fixedMin").value = fixed;
    $("randMin").value = r.randMin;
    $("randMax").value = r.randMax;
    $("retry").value = r.retrySecs;
  }
);

$("save").addEventListener("click", () => {
  const data = {
    checkMode: getMode(),
    fixedMin: parseFloat($("fixedMin").value) || 2,
    randMin: parseFloat($("randMin").value) || 2,
    randMax: parseFloat($("randMax").value) || 5,
    retrySecs: parseFloat($("retry").value) || 30,
  };
  chrome.storage.local.set(data, () => {
    $("status").textContent = "Saved";
    setTimeout(() => ($("status").textContent = ""), 1500);
  });
});
