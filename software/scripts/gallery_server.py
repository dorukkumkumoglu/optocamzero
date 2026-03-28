#!/usr/bin/env python3
"""OptoCam Gallery Server — serves photos over WiFi hotspot"""
import os
import io
import json
import time
import zipfile
from flask import Flask, send_from_directory, render_template_string, Response, request

PHOTOS_DIR = "/home/dkumkum/photos"

app = Flask(__name__)

HTML = """<!DOCTYPE html>
<html>
<head>
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">
<title>OptoCam</title>
<style>
@font-face {
    font-family: 'CamFont';
    src: url('/font/cmunvt.ttf') format('truetype');
}
* { margin: 0; padding: 0; box-sizing: border-box; }
body {
    background: #080808;
    color: #d0d0d0;
    font-family: 'CamFont', 'Courier New', monospace;
    padding: 14px;
    -webkit-tap-highlight-color: transparent;
    overscroll-behavior: none;
}
header {
    border-bottom: 1px solid #1e1e1e;
    padding-bottom: 12px;
    margin-bottom: 14px;
    display: flex;
    flex-direction: column;
    align-items: center;
}
.logo { height: 30px; width: auto; display: block; margin-top: 22px; margin-bottom: 22px; }
@media (min-width: 768px) {
    .logo { margin-top: 32px; }
}
.meta {
    width: 100%;
    display: flex;
    justify-content: space-between;
    font-size: 11px;
    color: #444;
    letter-spacing: 1px;
}
.grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 8px;
    padding-bottom: 50px;
}
@media (min-width: 768px) {
    .grid {
        grid-template-columns: repeat(4, 1fr);
    }
}
@media (min-width: 1200px) {
    .grid {
        grid-template-columns: repeat(5, 1fr);
        max-width: 1400px;
        margin: 0 auto;
    }
}
.item {
    background: #101010;
    border: 1px solid #1a1a1a;
    border-radius: 3px;
    overflow: hidden;
    cursor: pointer;
}
.item.sel { border-color: #1a1a1a; }
.img-wrap { position: relative; }
.img-btn {
    display: block;
    width: 100%;
    padding: 0;
    border: none;
    background: none;
    cursor: pointer;
}
.img-btn img {
    width: 100%;
    aspect-ratio: 1 / 1;
    object-fit: cover;
    display: block;
}
.sel-circle {
    position: absolute;
    top: 7px;
    left: 7px;
    width: 22px;
    height: 22px;
    border-radius: 50%;
    border: none;
    background: transparent;
    cursor: pointer;
    z-index: 2;
    transition: none;
}
.sel-circle::before {
    content: '';
    width: 22px;
    height: 22px;
    border-radius: 50%;
    border: 1.5px solid rgba(255,255,255,0.45);
    background: rgba(0,0,0,0.45);
    display: block;
    transition: background 0.12s, border-color 0.12s;
    box-sizing: border-box;
}
.item.sel .sel-circle::before {
    background: #fff;
    border-color: #fff;
}
.item.sel .sel-circle::after {
    content: '';
    position: absolute;
    top: 50%;
    left: 50%;
    width: 5px;
    height: 9px;
    border: 2px solid #000;
    border-top: none;
    border-left: none;
    transform: translate(-60%, -65%) rotate(45deg);
}
.dl-icon {
    position: absolute;
    top: 7px;
    right: 7px;
    width: 28px;
    height: 28px;
    background: rgba(0,0,0,0.55);
    border: 1px solid rgba(255,255,255,0.12);
    border-radius: 50%;
    display: flex;
    align-items: center;
    justify-content: center;
    text-decoration: none;
    z-index: 2;
}
.dl-icon svg { width: 13px; height: 13px; }

/* ── Viewer ── */
#viewer {
    position: fixed;
    inset: 0;
    background: #000;
    z-index: 100;
    flex-direction: column;
    display: none;
    pointer-events: none;
}
#viewer.open { display: flex; pointer-events: auto; }
.viewer-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 12px 14px;
    border-bottom: 1px solid #1a1a1a;
    flex-shrink: 0;
}
.viewer-back {
    font-family: 'CamFont', monospace;
    font-size: 13px;
    color: #aaa;
    background: none;
    border: none;
    cursor: pointer;
    letter-spacing: 1px;
    padding: 4px 0;
    display: flex;
    align-items: center;
    gap: 5px;
}
.viewer-pos {
    font-size: 16px;
    color: #aaa;
    letter-spacing: 1px;
}
.viewer-dl {
    width: 32px;
    height: 32px;
    background: #141414;
    border: 1px solid #2a2a2a;
    border-radius: 50%;
    display: flex;
    align-items: center;
    justify-content: center;
    text-decoration: none;
}
.viewer-dl svg { width: 14px; height: 14px; }
.viewer-hq {
    width: 32px;
    height: 32px;
    border-radius: 50%;
    border: 1px solid #2a2a2a;
    background: #141414;
    color: #383838;
    font-family: 'CamFont', monospace;
    font-size: 11px;
    letter-spacing: 0.5px;
    cursor: pointer;
    display: flex;
    align-items: center;
    justify-content: center;
    padding-left: 1px;
    transition: border-color 0.15s, color 0.15s;
}
.viewer-hq.active { border-color: #888; color: #aaa; }
.viewer-body {
    flex: 1;
    display: flex;
    align-items: center;
    justify-content: center;
    overflow: hidden;
    position: relative;
}
.viewer-body img {
    max-width: 100%;
    max-height: 100%;
    object-fit: contain;
}
.spinner {
    position: absolute;
    width: 32px;
    height: 32px;
    border: 2px solid #222;
    border-top-color: #888;
    border-radius: 50%;
    animation: spin 0.8s linear infinite;
    display: none;
}
.spinner.active { display: block; }
@keyframes spin { to { transform: rotate(360deg); } }
.viewer-nav {
    display: flex;
    border-top: 1px solid #1a1a1a;
    flex-shrink: 0;
    background: #000;
}
.nav-btn {
    flex: 1;
    padding: 14px;
    background: none;
    border: none;
    color: #666;
    font-family: 'CamFont', monospace;
    font-size: 18px;
    cursor: pointer;
    letter-spacing: 1px;
}
.nav-btn:active { background: #111; color: #fff; }
.nav-btn:first-child { border-right: 1px solid #1a1a1a; }
.side-nav { display: none; }
@media (min-width: 768px) {
    .viewer-nav { display: none; }
    .side-nav {
        display: flex;
        position: absolute;
        top: 50%;
        transform: translateY(-50%);
        width: 44px;
        height: 80px;
        align-items: center;
        justify-content: center;
        background: none;
        border: none;
        color: #555;
        font-size: 22px;
        cursor: pointer;
        transition: color 0.15s;
    }
    .side-nav:hover { color: #ccc; }
    .side-nav.left-nav { left: 12px; }
    .side-nav.right-nav { right: 12px; }
}
@media (min-width: 1200px) {
    .side-nav svg { width: 19px; height: 19px; }
}

/* ── Selection bar ── */
#sel-bar {
    position: fixed;
    bottom: 0; left: 0; right: 0;
    background: #080808;
    border-top: 1px solid #2a2a2a;
    padding: 10px 16px 12px;
    display: none;
    flex-direction: column;
    align-items: center;
    gap: 8px;
    z-index: 50;
}
@media (min-width: 768px) {
    #sel-bar { padding-bottom: 46px; }
}
#sel-bar.open { display: flex; }
.sel-top { display: flex; align-items: center; gap: 12px; margin: 4px 0; }
#sel-count { font-size: 12px; color: #666; letter-spacing: 1px; }
#desel-btn {
    display: flex;
    align-items: center;
    justify-content: center;
    background: none;
    border: none;
    cursor: pointer;
    padding: 0;
    width: 20px;
    height: 20px;
    transform: translateY(-1px);
}
#desel-btn svg { display: block; }
.sel-bar-btns { display: flex; gap: 8px; }
#dl-all-btn, #del-btn {
    font-family: 'CamFont', monospace;
    font-size: 12px;
    letter-spacing: 2px;
    background: #080808;
    border: 1px solid #333;
    border-radius: 3px;
    padding: 8px 16px;
    cursor: pointer;
}
#dl-all-btn { color: #fff; }
#del-btn { color: #e03030; border-color: #3a1a1a; }

/* ── Confirm popup ── */
#confirm-overlay {
    position: fixed;
    inset: 0;
    background: rgba(0,0,0,0.7);
    z-index: 200;
    display: none;
    pointer-events: none;
    align-items: center;
    justify-content: center;
}
#confirm-overlay.open { display: flex; pointer-events: auto; }
#confirm-box {
    background: #080808;
    border: 1px solid #2a2a2a;
    border-radius: 4px;
    padding: 24px 20px;
    width: 80%;
    max-width: 280px;
    text-align: center;
}
#confirm-box p {
    font-size: 12px;
    color: #aaa;
    letter-spacing: 1px;
    margin-bottom: 20px;
    line-height: 1.6;
}
.confirm-btns { display: flex; gap: 10px; }
.confirm-btns button {
    flex: 1;
    font-family: 'CamFont', monospace;
    font-size: 12px;
    letter-spacing: 2px;
    padding: 8px 16px;
    border-radius: 3px;
    cursor: pointer;
    background: #080808;
    border: 1px solid #333;
}
#confirm-yes { color: #e03030; border-color: #3a1a1a; }
#confirm-no  { color: #aaa; }
.empty {
    text-align: center;
    color: #333;
    font-size: 13px;
    letter-spacing: 2px;
    margin-top: 60px;
}
#top-btn {
    position: fixed;
    bottom: 20px;
    right: 16px;
    width: 38px;
    height: 38px;
    background: #141414;
    border: 1px solid #2a2a2a;
    border-radius: 50%;
    color: #888;
    display: none;
    align-items: center;
    justify-content: center;
    cursor: pointer;
    z-index: 40;
}
#top-btn.visible { display: flex; }
#drag-select {
    position: fixed;
    border: 1px solid rgba(255,255,255,0.35);
    background: rgba(255,255,255,0.05);
    pointer-events: none;
    z-index: 30;
    display: none;
}
</style>
</head>
<body>

<header>
  <img class="logo" src="/logo" alt="OptoCam">
  <div class="meta">
    <span>{{ count }} IMAGE{% if count != 1 %}S{% endif %}</span>
    <span>{{ free_space }} FREE</span>
  </div>
</header>

{% if files %}
<div class="grid" id="grid">
  {% for f in files %}
  <div class="item" data-file="{{ f }}" data-idx="{{ loop.index0 }}">
    <div class="img-wrap">
      <button class="img-btn" onclick="openViewer({{ loop.index0 }})">
        <img src="/thumb/{{ f }}" loading="lazy" alt="{{ f }}">
      </button>
      <button class="sel-circle" onclick="toggleSel(event, this)"></button>
      <a class="dl-icon" href="/photo/{{ f }}" download="{{ f }}" onclick="event.stopPropagation()">
        <svg viewBox="0 0 16 16" xmlns="http://www.w3.org/2000/svg">
          <path d="M8 1v9M4 7l4 4 4-4M2 14h12" stroke="#fff" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" fill="none"/>
        </svg>
      </a>
    </div>
  </div>
  {% endfor %}
</div>
{% else %}
<div class="empty">&gt; NO IMAGES_</div>
{% endif %}

<!-- Viewer -->
<div id="viewer">
  <div class="viewer-header" onclick="event.stopPropagation()">
    <button class="viewer-back" onclick="closeViewer()"><svg viewBox="0 0 16 16" width="13" height="13" xmlns="http://www.w3.org/2000/svg" style="position:relative;top:-1px;"><path d="M10 3L5 8l5 5" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" fill="none"/></svg>BACK</button>
    <div style="display:flex;flex-direction:column;align-items:center;gap:3px;">
      <span class="viewer-pos" id="viewer-pos"></span>
      <span id="viewer-filename" style="font-size:11px;color:#666;letter-spacing:0.5px;"></span>
    </div>
    <div style="display:flex;align-items:center;gap:8px;">
      <button class="viewer-hq" id="viewer-hq" onclick="toggleHQ()">HQ</button>
      <a class="viewer-dl" id="viewer-dl" href="#" download>
        <svg viewBox="0 0 16 16" xmlns="http://www.w3.org/2000/svg">
          <path d="M8 1v9M4 7l4 4 4-4M2 14h12" stroke="#fff" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" fill="none"/>
        </svg>
      </a>
    </div>
  </div>
  <div class="viewer-body" id="viewer-body" onclick="if(window.innerWidth>=1200)closeViewer()">
    <div class="spinner" id="spinner"></div>
    <img id="viewer-img" src="" alt="" onload="imgLoaded()" onclick="event.stopPropagation()" style="display:none;">
    <button class="side-nav left-nav" onclick="event.stopPropagation();stepViewer(-1)"><svg viewBox="0 0 16 16" width="16" height="16" xmlns="http://www.w3.org/2000/svg"><path d="M10 3L5 8l5 5" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" fill="none"/></svg></button>
    <button class="side-nav right-nav" onclick="event.stopPropagation();stepViewer(1)"><svg viewBox="0 0 16 16" width="16" height="16" xmlns="http://www.w3.org/2000/svg"><path d="M6 3l5 5-5 5" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" fill="none"/></svg></button>
  </div>
  <div class="viewer-nav">
    <button class="nav-btn" onclick="stepViewer(-1)"><svg viewBox="0 0 16 16" width="14" height="14" xmlns="http://www.w3.org/2000/svg"><path d="M10 3L5 8l5 5" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" fill="none"/></svg></button>
    <button class="nav-btn" onclick="stepViewer(1)"><svg viewBox="0 0 16 16" width="14" height="14" xmlns="http://www.w3.org/2000/svg"><path d="M6 3l5 5-5 5" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" fill="none"/></svg></button>
  </div>
</div>

<!-- Footer -->
<footer style="text-align:center; padding: 28px 0 20px; font-size:11px; color:#444; letter-spacing:1px;">Doruk Kumkumo&#287;lu 2026</footer>

<!-- Drag selection box -->
<div id="drag-select"></div>

<!-- Back to top -->
<button id="top-btn" onclick="window.scrollTo({top:0,behavior:'smooth'})"><svg viewBox="0 0 16 16" width="14" height="14" xmlns="http://www.w3.org/2000/svg"><path d="M3 10l5-5 5 5" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" fill="none"/></svg></button>

<!-- Selection bar -->
<div id="sel-bar">
  <div class="sel-top">
    <span id="sel-count"></span>
    <button id="desel-btn" onclick="deselectAll()">
      <svg viewBox="0 0 16 16" width="14" height="14" xmlns="http://www.w3.org/2000/svg">
        <path d="M3 3l10 10M13 3L3 13" stroke="#555" stroke-width="1.8" stroke-linecap="round"/>
      </svg>
    </button>
  </div>
  <div class="sel-bar-btns">
    <button id="dl-all-btn" onclick="downloadSelected()">DOWNLOAD ALL</button>
    <button id="del-btn" onclick="confirmDelete()">DELETE ALL</button>
  </div>
</div>

<!-- Confirm delete popup -->
<div id="confirm-overlay" style="display:none">
  <div id="confirm-box">
    <p id="confirm-msg"></p>
    <div class="confirm-btns">
      <button id="confirm-yes" onclick="deleteSelected()">DELETE</button>
      <button id="confirm-no" onclick="closeConfirm()">CANCEL</button>
    </div>
  </div>
</div>

<script>
const files = {{ files_json | safe }};
const selected = new Set();
let viewerIdx = 0;
let hqMode = false;


function toggleSel(e, circle) {
    e.stopPropagation();
    const item = circle.closest('.item');
    const f = item.dataset.file;
    if (selected.has(f)) { selected.delete(f); item.classList.remove('sel'); }
    else { selected.add(f); item.classList.add('sel'); }
    const bar = document.getElementById('sel-bar');
    if (selected.size > 0) {
        bar.classList.add('open');
        document.getElementById('sel-count').textContent = selected.size + ' SELECTED';
    } else {
        bar.classList.remove('open');
    }
}

function openViewer(idx) {
    viewerIdx = idx;
    hqMode = false;
    document.getElementById('viewer-hq').classList.remove('active');
    updateViewer();
    document.getElementById('viewer').classList.add('open');
    document.body.style.overflow = 'hidden';
}

function closeViewer() {
    document.getElementById('viewer').classList.remove('open');
    document.body.style.overflow = '';
    hqMode = false;
    document.getElementById('viewer-hq').classList.remove('active');
}

function toggleHQ() {
    hqMode = !hqMode;
    document.getElementById('viewer-hq').classList.toggle('active', hqMode);
    const img = document.getElementById('viewer-img');
    const spinner = document.getElementById('spinner');
    const f = files[viewerIdx];
    img.style.display = 'none';
    spinner.classList.add('active');
    img.src = hqMode ? '/photo/' + f : '/thumb/' + f + '?size=1200';
}

function updateViewer() {
    const img = document.getElementById('viewer-img');
    const spinner = document.getElementById('spinner');
    const f = files[viewerIdx];
    img.style.display = 'none';
    spinner.classList.add('active');
    img.src = hqMode ? '/photo/' + f : '/thumb/' + f + '?size=1200';
    document.getElementById('viewer-pos').textContent = (viewerIdx + 1) + ' / ' + files.length;
    document.getElementById('viewer-filename').textContent = f;
    const dl = document.getElementById('viewer-dl');
    dl.href = '/photo/' + f;
    dl.download = f;
}

function imgLoaded() {
    document.getElementById('spinner').classList.remove('active');
    document.getElementById('viewer-img').style.display = 'block';
}

window.addEventListener('load', () => fetch('/preload'));

window.addEventListener('scroll', () => {
    const btn = document.getElementById('top-btn');
    btn.classList.toggle('visible', window.scrollY > 300);
});

function stepViewer(dir) {
    const next = viewerIdx + dir;
    if (next >= 0 && next < files.length) {
        viewerIdx = next;
        hqMode = false;
        document.getElementById('viewer-hq').classList.remove('active');
        updateViewer();
    }
}

// Keyboard navigation in viewer
document.addEventListener('keydown', e => {
    const isDesktop = window.innerWidth >= 1200;
    const viewerOpen = document.getElementById('viewer').classList.contains('open');
    if (viewerOpen) {
        if (e.key === 'ArrowLeft') stepViewer(-1);
        else if (e.key === 'ArrowRight') stepViewer(1);
        else if (e.key === 'Escape') closeViewer();
    } else if (isDesktop) {
        if (e.key === 'Escape' && selected.size > 0) deselectAll();
        else if (e.key === 'Backspace' && selected.size > 0) { e.preventDefault(); confirmDelete(); }
    }
});

// Swipe gestures in viewer
let touchStartX = 0, touchStartY = 0, multiTouch = false;
const vb = document.getElementById('viewer-body');
vb.addEventListener('touchstart', e => {
    multiTouch = e.touches.length > 1;
    touchStartX = e.touches[0].clientX;
    touchStartY = e.touches[0].clientY;
}, {passive:true});
vb.addEventListener('touchmove', e => {
    if (e.touches.length > 1) multiTouch = true;
}, {passive:true});
vb.addEventListener('touchend', e => {
    if (multiTouch) { multiTouch = false; return; }
    const dx = e.changedTouches[0].clientX - touchStartX;
    const dy = e.changedTouches[0].clientY - touchStartY;
    if (Math.abs(dy) > Math.abs(dx) && dy > 60) { closeViewer(); return; }
    if (Math.abs(dx) > 50) {
        if (dx < 0) stepViewer(1);
        else stepViewer(-1);
    }
}, {passive:true});

function deselectAll() {
    selected.forEach(f => {
        const el = document.querySelector(`.item[data-file="${f}"]`);
        if (el) el.classList.remove('sel');
    });
    selected.clear();
    document.getElementById('sel-bar').classList.remove('open');
}

function downloadSelected() {
    const form = document.createElement('form');
    form.method = 'POST';
    form.action = '/download-zip';
    selected.forEach(f => {
        const inp = document.createElement('input');
        inp.type = 'hidden';
        inp.name = 'files';
        inp.value = f;
        form.appendChild(inp);
    });
    document.body.appendChild(form);
    form.submit();
}

function confirmDelete() {
    const n = selected.size;
    document.getElementById('confirm-msg').textContent =
        'Delete ' + n + ' image' + (n !== 1 ? 's' : '') + '? This cannot be undone.';
    const ov = document.getElementById('confirm-overlay');
    ov.style.display = 'flex';
    ov.style.pointerEvents = 'auto';
}

function closeConfirm() {
    const ov = document.getElementById('confirm-overlay');
    ov.style.display = 'none';
    ov.style.pointerEvents = 'none';
}

async function deleteSelected() {
    closeConfirm();
    const filesToDelete = [...selected];
    const res = await fetch('/delete', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({files: filesToDelete})
    });
    if (res.ok) {
        filesToDelete.forEach(f => {
            const el = document.querySelector(`.item[data-file="${f}"]`);
            if (el) el.remove();
            selected.delete(f);
            const idx = files.indexOf(f);
            if (idx !== -1) files.splice(idx, 1);
        });
        document.getElementById('sel-bar').classList.remove('open');
        const count = document.querySelectorAll('.item').length;
        document.querySelector('.meta span').textContent =
            count + ' IMAGE' + (count !== 1 ? 'S' : '');
    }
}

// Drag-to-select (desktop only)
let dragStart = null, dragMoved = false;
const dragBox = document.getElementById('drag-select');

document.addEventListener('mousedown', e => {
    if (window.innerWidth < 1200) return;
    if (document.getElementById('viewer').classList.contains('open')) return;
    const blocked = e.target.closest('.img-btn,.sel-circle,.dl-icon,#sel-bar,#top-btn,header');
    if (blocked) return;
    dragStart = { x: e.clientX, y: e.clientY };
    dragMoved = false;
    document.body.style.userSelect = 'none';
});

document.addEventListener('mousemove', e => {
    if (!dragStart) return;
    const dx = e.clientX - dragStart.x, dy = e.clientY - dragStart.y;
    if (!dragMoved && Math.abs(dx) < 5 && Math.abs(dy) < 5) return;
    dragMoved = true;
    const x1 = Math.min(dragStart.x, e.clientX);
    const y1 = Math.min(dragStart.y, e.clientY);
    const x2 = Math.max(dragStart.x, e.clientX);
    const y2 = Math.max(dragStart.y, e.clientY);
    dragBox.style.cssText = `display:block;left:${x1}px;top:${y1}px;width:${x2-x1}px;height:${y2-y1}px;`;
});

document.addEventListener('mouseup', e => {
    if (!dragStart) return;
    document.body.style.userSelect = '';
    dragBox.style.display = 'none';
    if (dragMoved) {
        const x1 = Math.min(dragStart.x, e.clientX);
        const y1 = Math.min(dragStart.y, e.clientY);
        const x2 = Math.max(dragStart.x, e.clientX);
        const y2 = Math.max(dragStart.y, e.clientY);
        document.querySelectorAll('.item').forEach(item => {
            const r = item.getBoundingClientRect();
            if (r.left < x2 && r.right > x1 && r.top < y2 && r.bottom > y1) {
                const f = item.dataset.file;
                if (!selected.has(f)) { selected.add(f); item.classList.add('sel'); }
            }
        });
        if (selected.size > 0) {
            document.getElementById('sel-bar').classList.add('open');
            document.getElementById('sel-count').textContent = selected.size + ' SELECTED';
        }
    }
    dragStart = null;
    dragMoved = false;
});
</script>
</body>
</html>"""


def get_free_space():
    try:
        path = PHOTOS_DIR if os.path.exists(PHOTOS_DIR) else "/home/dkumkum"
        stat = os.statvfs(path)
        free = stat.f_bavail * stat.f_bsize
        if free >= 1024 ** 3:
            return f"{free / 1024**3:.1f} GB"
        return f"{free / 1024**2:.0f} MB"
    except:
        return "?"


@app.route("/")
def index():
    if os.path.exists(PHOTOS_DIR):
        files = sorted(
            [f for f in os.listdir(PHOTOS_DIR) if f.lower().endswith(".jpg")],
            key=lambda f: int(f[len("Optocamzero_"):-len(".jpg")]) if f.startswith("Optocamzero_") and f[len("Optocamzero_"):-len(".jpg")].isdigit() else 0,
            reverse=True
        )
    else:
        files = []
    return render_template_string(
        HTML, files=files, count=len(files),
        free_space=get_free_space(), files_json=json.dumps(files)
    )


@app.route("/logo")
def logo():
    return send_from_directory("/home/dkumkum", "optocamlogo.svg", mimetype="image/svg+xml")


@app.route("/font/<filename>")
def font(filename):
    return send_from_directory("/home/dkumkum", filename)


@app.route("/photo/<filename>")
def photo(filename):
    return send_from_directory(PHOTOS_DIR, filename, as_attachment=True)


THUMB_DIR = os.path.join(PHOTOS_DIR, ".thumbs")

def get_thumb_path(filename, size):
    os.makedirs(THUMB_DIR, exist_ok=True)
    return os.path.join(THUMB_DIR, f"{filename}_{size}.jpg")

@app.route("/thumb/<filename>")
def thumb(filename):
    path = os.path.join(PHOTOS_DIR, filename)
    if not os.path.exists(path):
        return "Not found", 404
    size = min(request.args.get('size', 400, type=int), 1200)
    cache_path = get_thumb_path(filename, size)
    if not os.path.exists(cache_path):
        from PIL import Image
        img = Image.open(path)
        img.thumbnail((size, size))
        img.save(cache_path, "JPEG", quality=75)
    with open(cache_path, "rb") as f:
        return Response(f.read(), mimetype="image/jpeg")


@app.route("/preload")
def preload():
    import threading
    from PIL import Image as PILImage
    def generate_all():
        if not os.path.exists(PHOTOS_DIR):
            return
        files = sorted(
            [f for f in os.listdir(PHOTOS_DIR) if f.lower().endswith(".jpg")],
            key=lambda f: int(f[len("Optocamzero_"):-len(".jpg")]) if f.startswith("Optocamzero_") and f[len("Optocamzero_"):-len(".jpg")].isdigit() else 0,
            reverse=True
        )[:50]  # only preload 50 most recent
        for filename in files:
            cache_path = get_thumb_path(filename, 1200)
            if os.path.exists(cache_path):
                continue
            try:
                img = PILImage.open(os.path.join(PHOTOS_DIR, filename))
                img.thumbnail((1200, 1200))
                img.save(cache_path, "JPEG", quality=75)
            except:
                pass
            time.sleep(0.2)  # keep server responsive between generations
    threading.Thread(target=generate_all, daemon=True).start()
    return "", 204


@app.route("/delete", methods=["POST"])
def delete_photos():
    data = request.get_json()
    filenames = data.get("files", [])
    for f in filenames:
        path = os.path.join(PHOTOS_DIR, os.path.basename(f))
        if os.path.exists(path):
            os.remove(path)
        for size in [400, 1200]:
            cache = get_thumb_path(os.path.basename(f), size)
            if os.path.exists(cache):
                os.remove(cache)
    return "", 204


@app.route("/download-zip", methods=["POST"])
def download_zip():
    filenames = request.form.getlist("files")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        for f in filenames:
            path = os.path.join(PHOTOS_DIR, f)
            if os.path.exists(path):
                zf.write(path, f)
    buf.seek(0)
    return Response(
        buf.getvalue(),
        mimetype="application/zip",
        headers={"Content-Disposition": "attachment; filename=optocam_photos.zip"}
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=80, debug=False)
