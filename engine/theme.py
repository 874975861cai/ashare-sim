# -*- coding: utf-8 -*-
"""看板视觉主题：科幻 HUD 风格。

抽成独立文件，换风格只改这里，不动渲染逻辑。
所有颜色集中在上面的 :root 变量里，改配色不用翻代码。

设计约束：
  - 自包含，不引用任何外部字体/图片/CDN
  - 涨红跌绿（A 股惯例），但做成霓虹色而非纯红纯绿
  - 深色底 + 亮字，保证数据可读性优先于炫技
  - 动画克制：只有状态点和顶部扫描带，不晃眼
"""

PAGE_CSS = """
  *{box-sizing:border-box;margin:0;padding:0}
  :root{
    --bg:#05070d;
    --panel-a:rgba(16,26,44,.90);
    --panel-b:rgba(7,11,19,.92);
    --line:rgba(80,150,255,.15);
    --line-soft:rgba(80,150,255,.07);
    --cyan:#22d3ee;
    --cyan-dim:#7fd8f0;
    --violet:#7c9dff;
    --txt:#dbe7f5;
    --txt-2:#c7d6e8;
    --dim:#7b90ad;
    --dim-2:#5f7fa8;
    --up:#ff4d6a;
    --down:#22e6a0;
    --amber:#f5a524;
  }

  body{
    background:
      radial-gradient(1100px 560px at 50% -12%, rgba(34,211,238,.10), transparent 62%),
      radial-gradient(760px 420px at 102% 104%, rgba(124,58,237,.09), transparent 62%),
      var(--bg);
    color:var(--txt);
    font-family:-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;
    padding:26px 22px 40px;
    min-height:100vh;
    position:relative;
    overflow-x:hidden;
  }

  /* 背景点阵网格，向下渐隐 */
  body::before{
    content:"";position:fixed;inset:0;pointer-events:none;z-index:0;
    background-image:
      linear-gradient(rgba(80,150,255,.085) 1px, transparent 1px),
      linear-gradient(90deg, rgba(80,150,255,.085) 1px, transparent 1px);
    background-size:40px 40px;
    -webkit-mask-image:radial-gradient(ellipse at 50% 0%, #000 48%, transparent 94%);
    mask-image:radial-gradient(ellipse at 50% 0%, #000 48%, transparent 94%);
  }

  /* 缓慢下移的扫描光带 */
  body::after{
    content:"";position:fixed;left:0;right:0;height:150px;top:-150px;
    background:linear-gradient(180deg,transparent,rgba(34,211,238,.045),transparent);
    animation:scan 13s linear infinite;
    pointer-events:none;z-index:0;
  }
  @keyframes scan{0%{top:-150px}100%{top:100%}}
  @keyframes pulse{0%,100%{opacity:1;transform:scale(1)}50%{opacity:.45;transform:scale(.82)}}
  @keyframes spin{100%{transform:rotate(360deg)}}
  @keyframes halo{
    0%,100%{transform:scale(.92);opacity:.5}
    50%{transform:scale(1.07);opacity:1}
  }

  .wrap{max-width:1240px;margin:0 auto;position:relative;z-index:1}

  header{
    display:flex;justify-content:space-between;align-items:flex-end;
    margin-bottom:20px;flex-wrap:wrap;gap:12px;
    padding-bottom:14px;border-bottom:1px solid var(--line);
    position:relative;
  }
  header::after{
    content:"";position:absolute;left:0;bottom:-1px;width:180px;height:1px;
    background:linear-gradient(90deg,var(--cyan),transparent);
    box-shadow:0 0 10px rgba(34,211,238,.55);
  }
  h1{
    font-size:20px;font-weight:700;letter-spacing:2.4px;
    background:linear-gradient(90deg,#eaf7ff,#22d3ee 52%,#8fa9ff);
    -webkit-background-clip:text;background-clip:text;
    -webkit-text-fill-color:transparent;color:transparent;
    filter:drop-shadow(0 0 18px rgba(34,211,238,.34));
  }
  h1::before{
    content:"";display:inline-block;width:4px;height:15px;margin-right:11px;
    vertical-align:-1px;
    background:linear-gradient(180deg,var(--cyan),var(--violet));
    box-shadow:0 0 13px rgba(34,211,238,.85);
  }
  h1 span{
    font-size:12px;font-weight:400;letter-spacing:.6px;margin-left:12px;
    -webkit-text-fill-color:var(--dim);color:var(--dim);
  }
  .stamp{font-size:12px;color:var(--dim);letter-spacing:.4px}
  .stamp .warn{
    font-style:normal;color:#f7c46c;background:rgba(245,165,36,.11);
    border:1px solid rgba(245,165,36,.26);
    padding:1px 7px;border-radius:4px;font-size:11px;
  }
  .dot{
    display:inline-block;width:7px;height:7px;border-radius:50%;
    background:var(--down);margin-right:7px;vertical-align:1px;
    box-shadow:0 0 9px var(--down);animation:pulse 2s ease-in-out infinite;
  }

  .kpis{display:grid;grid-template-columns:repeat(5,1fr);gap:12px;margin-bottom:14px}
  .kpi{
    position:relative;overflow:hidden;
    background:linear-gradient(160deg,var(--panel-a),var(--panel-b));
    border:1px solid var(--line);border-radius:9px;
    padding:15px 15px 14px;
    box-shadow:inset 0 1px 0 rgba(255,255,255,.045), 0 10px 28px rgba(0,0,0,.45);
    transition:border-color .25s, box-shadow .25s, transform .25s;
  }
  .kpi:hover{
    border-color:rgba(34,211,238,.34);
    box-shadow:inset 0 1px 0 rgba(255,255,255,.06),
               0 10px 30px rgba(0,0,0,.5),
               0 0 26px rgba(34,211,238,.12);
    transform:translateY(-1px);
  }
  .kpi::before{
    content:"";position:absolute;top:0;left:0;right:0;height:1px;
    background:linear-gradient(90deg,transparent,rgba(34,211,238,.70),transparent);
  }
  .kpi::after{
    content:"";position:absolute;right:-26px;bottom:-26px;width:78px;height:78px;
    background:radial-gradient(circle,rgba(34,211,238,.15),transparent 70%);
    pointer-events:none;
  }
  .kpi .lab{
    font-size:10.5px;color:var(--dim-2);margin-bottom:8px;
    letter-spacing:1.4px;text-transform:uppercase;
  }
  .kpi .val{
    font-size:23px;font-weight:700;letter-spacing:.4px;
    font-family:ui-monospace,Consolas,"SF Mono",monospace;
    font-variant-numeric:tabular-nums;color:#eaf6ff;
    text-shadow:0 0 18px rgba(34,211,238,.26);
  }
  .kpi .sub{font-size:11px;color:var(--dim);margin-top:6px}

  .card{
    position:relative;
    background:linear-gradient(170deg,var(--panel-a),var(--panel-b));
    border:1px solid var(--line);border-radius:9px;
    padding:16px 18px 18px;margin-bottom:14px;
    box-shadow:0 12px 32px rgba(0,0,0,.45),
               0 0 26px rgba(34,211,238,.035),
               inset 0 1px 0 rgba(255,255,255,.03);
    transition:border-color .25s, box-shadow .25s;
  }
  .card:hover{
    border-color:rgba(34,211,238,.30);
    box-shadow:0 12px 34px rgba(0,0,0,.5),
               0 0 30px rgba(34,211,238,.09),
               inset 0 1px 0 rgba(255,255,255,.05);
  }
  .card::before{
    content:"";position:absolute;top:-1px;left:-1px;width:20px;height:20px;
    border-top:1px solid rgba(34,211,238,.72);
    border-left:1px solid rgba(34,211,238,.72);
    border-top-left-radius:9px;pointer-events:none;
  }
  .card::after{
    content:"";position:absolute;right:-1px;bottom:-1px;width:20px;height:20px;
    border-bottom:1px solid rgba(34,211,238,.32);
    border-right:1px solid rgba(34,211,238,.32);
    border-bottom-right-radius:9px;pointer-events:none;
  }
  .card h2{
    font-size:12.5px;font-weight:600;color:#9fb6d4;
    letter-spacing:1.5px;text-transform:uppercase;
    margin-bottom:13px;display:flex;justify-content:space-between;align-items:center;
  }
  .card h2 em{
    font-style:normal;font-weight:400;font-size:10.5px;
    color:var(--dim-2);letter-spacing:.4px;text-transform:none;
  }

  table{width:100%;border-collapse:collapse;font-size:12.5px}
  /* 手机/平板上宽表格（策略监控 9 列）会顶出卡片，而 body 是 overflow-x:hidden，
     多出来的列会直接被裁掉、还看不到滚动条。包一层自己滚，列宽才保得住。 */
  .tscroll{width:100%}
  th{
    text-align:left;font-weight:500;color:var(--dim-2);font-size:10px;
    letter-spacing:1.2px;text-transform:uppercase;
    padding:8px 8px;border-bottom:1px solid var(--line);white-space:nowrap;
  }
  td{
    padding:9px 8px;border-bottom:1px solid var(--line-soft);
    font-variant-numeric:tabular-nums;color:var(--txt-2);
  }
  tr:last-child td{border-bottom:none}
  tbody tr{transition:background .18s}
  tbody tr:hover td{background:rgba(56,189,248,.055)}

  .num{text-align:right;font-variant-numeric:tabular-nums}
  .mono{font-variant-numeric:tabular-nums;color:var(--dim);
    font-family:ui-monospace,Consolas,monospace;font-size:11.5px}
  .code{
    font-family:ui-monospace,Consolas,monospace;font-size:11.5px;
    color:var(--cyan-dim);background:rgba(34,211,238,.085);
    border:1px solid rgba(34,211,238,.16);
    padding:1px 5px;border-radius:3px;
  }
  .nm{color:var(--txt)}

  .up{color:var(--up);font-weight:600;text-shadow:0 0 12px rgba(255,77,106,.34)}
  .down{color:var(--down);font-weight:600;text-shadow:0 0 12px rgba(34,230,160,.30)}
  .flat{color:var(--dim)}
  .muted{color:#4a5f7a}
  .empty{text-align:center;color:var(--dim-2);padding:28px 0;font-size:12.5px;
    letter-spacing:.5px}

  /* 执行状态条：把「这一刻的条件」和「账本执行到哪」分开说清楚。
     没有它的时候，页面写「可入场」而持仓空空，看着像系统坏了。 */
  .execnote{font-size:11.5px;line-height:1.75;letter-spacing:.3px;
    padding:9px 12px;border-radius:8px;margin:0 0 12px;
    border-left:3px solid var(--dim-2);
    background:rgba(80,150,255,.05);color:var(--dim)}
  .execnote.ok{border-left-color:var(--down);background:rgba(34,230,160,.06);
    color:#8fd6bd}
  .execnote.warn{border-left-color:#ffb02e;background:rgba(255,176,46,.08);
    color:#efc67d}
  .execnote.alert{border-left-color:var(--up);background:rgba(255,77,106,.09);
    color:#f2a2b0}
  .execnote b{color:#fff;font-weight:600}

  .tag{font-size:10.5px;padding:2px 7px;border-radius:3px;font-weight:600;
    letter-spacing:.5px}
  .tag.buy{background:rgba(255,77,106,.13);color:#ff8fa3;
    border:1px solid rgba(255,77,106,.30)}
  .tag.sell{background:rgba(34,230,160,.11);color:#5ff0bd;
    border:1px solid rgba(34,230,160,.28)}

  .reason{color:var(--dim-2);font-size:11.5px;max-width:230px;line-height:1.5}

  .pill{font-size:10.5px;padding:2px 9px;border-radius:12px;white-space:nowrap;
    letter-spacing:.5px;border:1px solid transparent}
  .pill.hot{background:rgba(255,77,106,.16);color:#ff9aab;
    border-color:rgba(255,77,106,.48);
    box-shadow:0 0 16px rgba(255,77,106,.28);animation:pulse 1.6s ease-in-out infinite}
  .pill.warm{background:rgba(245,165,36,.10);color:#f7c46c;
    border-color:rgba(245,165,36,.26)}
  .pill.cold{background:rgba(120,150,190,.09);color:#8aa0bd;
    border-color:rgba(120,150,190,.18)}

  .curve{width:100%;height:auto;display:block;
    filter:drop-shadow(0 0 14px rgba(34,211,238,.10))}

  /* ==================== 资金流场 ==================== */
  .orbit-card{padding-bottom:12px;overflow:hidden}
  .orbit{
    position:relative;width:100%;height:650px;margin:4px auto 0;
    max-width:860px;
  }
  /* 环形轨道与节点同心：74%/70% 正好等于节点所在的椭圆（RX 37% / RY 35%） */
  .orbit::before{
    content:"";position:absolute;left:50%;top:50%;
    width:74%;height:70%;
    transform:translate(-50%,-50%);border-radius:50%;
    border:1px solid rgba(80,150,255,.14);
    box-shadow:inset 0 0 70px rgba(34,211,238,.055);
    z-index:0;
  }
  .orbit::after{
    content:"";position:absolute;left:50%;top:50%;
    width:86%;height:82%;
    transform:translate(-50%,-50%);border-radius:50%;
    border:1px dashed rgba(80,150,255,.075);
    animation:spin 70s linear infinite;
    z-index:0;
  }
  #flow{
    position:absolute;inset:0;pointer-events:none;z-index:1;
  }

  .core{
    position:absolute;left:50%;top:50%;
    width:212px;height:212px;margin:-106px 0 0 -106px;
    z-index:3;display:flex;align-items:center;justify-content:center;
  }
  .core-halo{
    position:absolute;inset:-46px;border-radius:50%;
    background:radial-gradient(circle,rgba(34,211,238,.20),
      rgba(34,211,238,.055) 46%,transparent 72%);
    animation:halo 4.2s ease-in-out infinite;
  }
  .core-tick{
    position:absolute;inset:0;border-radius:50%;
    border:1px dashed rgba(34,211,238,.28);
    animation:spin 26s linear infinite;
  }
  .core-tick2{
    position:absolute;inset:15px;border-radius:50%;
    border:1px solid rgba(124,157,255,.18);
    border-top-color:rgba(34,211,238,.8);
    border-right-color:rgba(34,211,238,.38);
    animation:spin 9s linear infinite reverse;
  }
  .core-inner{
    position:relative;z-index:2;
    width:172px;height:172px;border-radius:50%;
    background:radial-gradient(circle at 50% 38%,rgba(17,32,54,.99),rgba(5,9,16,.99));
    border:1px solid rgba(34,211,238,.30);
    box-shadow:0 0 38px rgba(34,211,238,.17),inset 0 0 32px rgba(34,211,238,.07);
    display:flex;flex-direction:column;align-items:center;justify-content:center;
  }
  .core-lab{
    font-size:8.5px;letter-spacing:2.6px;color:#5f7fa8;margin-bottom:7px;
  }
  .core-val{
    font-family:ui-monospace,Consolas,monospace;font-size:20px;font-weight:700;
    color:#eaf7ff;letter-spacing:.2px;
    text-shadow:0 0 22px rgba(34,211,238,.60);
  }
  .core-day{font-size:12px;margin-top:7px;font-weight:600}
  .core-tot{font-size:10px;margin-top:3px;color:var(--dim);letter-spacing:.4px;
    text-shadow:none}
  .core-tot.up{color:var(--up);text-shadow:0 0 12px rgba(255,77,106,.34)}
  .core-tot.down{color:var(--down);text-shadow:0 0 12px rgba(34,230,160,.30)}

  .node{
    position:absolute;width:104px;margin:-33px 0 0 -52px;z-index:2;
    text-align:center;padding:6px 4px 7px;
    background:linear-gradient(160deg,rgba(16,26,44,.94),rgba(6,10,18,.96));
    border:1px solid rgba(80,150,255,.17);border-radius:7px;
    box-shadow:0 6px 18px rgba(0,0,0,.5);
    transition:border-color .3s,box-shadow .3s;
  }
  .node::before{
    content:"";position:absolute;top:-1px;left:-1px;width:10px;height:10px;
    border-top:1px solid rgba(34,211,238,.55);
    border-left:1px solid rgba(34,211,238,.55);
    border-top-left-radius:7px;
  }
  .node.held{
    border-color:rgba(255,77,106,.48);
    box-shadow:0 0 24px rgba(255,77,106,.22),0 6px 18px rgba(0,0,0,.5);
  }
  .node.sig{
    border-color:rgba(34,230,160,.55);
    animation:nodepulse 1.8s ease-in-out infinite;
  }
  @keyframes nodepulse{
    0%,100%{box-shadow:0 0 16px rgba(34,230,160,.20),0 6px 18px rgba(0,0,0,.5)}
    50%{box-shadow:0 0 32px rgba(34,230,160,.55),0 6px 18px rgba(0,0,0,.5)}
  }
  .nd-code{
    font-family:ui-monospace,Consolas,monospace;font-size:9px;
    color:#7fd8f0;letter-spacing:.6px;
  }
  .nd-px{
    font-family:ui-monospace,Consolas,monospace;font-size:14px;font-weight:700;
    color:#eaf6ff;margin:1px 0;text-shadow:0 0 12px rgba(34,211,238,.28);
  }
  .nd-pct{font-size:10.5px;font-weight:600}
  .nd-dev{font-size:9px;color:#5f7fa8;margin-top:2px;letter-spacing:.3px}

  .orbit-stats{
    display:grid;grid-template-columns:repeat(5,1fr);gap:10px;
    margin-top:12px;padding-top:14px;border-top:1px solid var(--line-soft);
  }
  .orbit-stats div{text-align:center}
  .orbit-stats span{
    display:block;font-size:9px;letter-spacing:1.5px;color:#5f7fa8;
    text-transform:uppercase;margin-bottom:5px;
  }
  .orbit-stats b{
    font-family:ui-monospace,Consolas,monospace;font-size:13px;
    color:var(--txt-2);font-weight:600;
  }


  .two{display:grid;grid-template-columns:1fr 1fr;gap:14px}

  .rule{font-size:12px;color:var(--dim);line-height:2.1}
  .rule b{color:var(--cyan-dim);font-weight:600;letter-spacing:.5px}
  .rule span{
    display:inline-block;background:rgba(34,211,238,.075);
    border:1px solid rgba(34,211,238,.15);border-radius:3px;padding:0 6px;
    font-family:ui-monospace,Consolas,monospace;font-size:11.5px;
    color:var(--cyan-dim);
  }

  footer{
    text-align:center;color:#3f5470;font-size:10.5px;
    margin-top:20px;letter-spacing:1.2px;
  }

  @media(max-width:900px){
    .kpis{grid-template-columns:repeat(2,1fr)}
    .two{grid-template-columns:1fr}
    .tscroll{overflow-x:auto;-webkit-overflow-scrolling:touch}
    .tscroll table{min-width:max-content}
    .orbit{height:520px}
    .node{width:84px;margin:-29px 0 0 -42px;padding:5px 3px 6px}
    .nd-px{font-size:12.5px}
    .core{width:170px;height:170px;margin:-85px 0 0 -85px}
    .core-inner{width:138px;height:138px}
    .core-val{font-size:16px}
    .orbit-stats{grid-template-columns:repeat(3,1fr);row-gap:12px}
  }
  @media(max-width:560px){
    .orbit{height:440px}
    .node{width:72px;margin:-26px 0 0 -36px}
    .nd-px{font-size:11.5px}
    .nd-pct,.nd-dev{font-size:8.5px}
    .core{width:132px;height:132px;margin:-66px 0 0 -66px}
    .core-inner{width:106px;height:106px}
    .core-val{font-size:13px}
    .core-lab{font-size:7px;letter-spacing:1.6px}
    .orbit-stats{grid-template-columns:repeat(2,1fr)}
  }

  /* ==================== 未连上实时服务的横幅 ==================== */
  #livebar{
    margin:0 0 14px;padding:12px 16px;border-radius:9px;
    background:linear-gradient(90deg,rgba(255,77,106,.16),rgba(255,77,106,.05));
    border:1px solid rgba(255,77,106,.42);
    color:#ffd9df;font-size:12.5px;line-height:1.7;
    box-shadow:0 0 22px rgba(255,77,106,.12);
    animation:bannerIn .28s ease-out;
  }
  #livebar b{color:#ff7c93;font-weight:700}
  @keyframes bannerIn{from{opacity:0;transform:translateY(-6px)}to{opacity:1;transform:none}}
"""
