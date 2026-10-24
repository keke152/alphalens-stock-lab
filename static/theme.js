"use strict";
(() => {
  let theme="light";
  try {
    const saved=document.cookie.match(/(?:^|;\s*)alphalens-theme=(light|dark)(?:;|$)/)?.[1] || localStorage.getItem("alphalens-theme");
    if(saved==="dark")theme="dark";
  } catch (_) {}
  document.documentElement.dataset.theme=theme;
  function update() {
    const button=document.getElementById("theme-toggle");
    if(button){button.textContent=theme==="dark"?"☀ 浅色模式":"☾ 深色行情";button.setAttribute("aria-pressed",String(theme==="dark"));}
  }
  document.addEventListener("DOMContentLoaded",()=>{
    update();
    document.getElementById("theme-toggle").addEventListener("click",()=>{
      theme=theme==="light"?"dark":"light";document.documentElement.dataset.theme=theme;
      try{localStorage.setItem("alphalens-theme",theme);document.cookie=`alphalens-theme=${theme}; Path=/; Max-Age=31536000; SameSite=Strict`;}catch(_){}
      update();if(typeof updateThreshold==="function")updateThreshold();
      requestAnimationFrame(()=>{if(typeof drawCharts==="function")drawCharts();});
    });
  });
})();
