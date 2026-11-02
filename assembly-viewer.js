// assembly-viewer.js
// 로컬 PC의 STL Pack(ZIP 또는 압축 해제된 폴더)을 브라우저 안에서만 읽어
// hierarchy.json 기반 부품 트리 + Three.js 3D 뷰어로 보여주는 클라이언트 전용 스크립트.
// 서버로 어떤 파일도 전송하지 않으며, 저장소에도 3D 원본 데이터를 커밋하지 않는다.

import * as THREE from 'three';
import { STLLoader } from 'three/addons/loaders/STLLoader.js';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { unzip } from 'fflate';

// ---------- DOM ----------
const zipInput = document.getElementById('zipInput');
const folderInput = document.getElementById('folderInput');
const sourceStatus = document.getElementById('sourceStatus');
const loadingBanner = document.getElementById('loadingBanner');
const treeRoot = document.getElementById('treeRoot');
const statParts = document.getElementById('statParts');
const statTris = document.getElementById('statTris');
const clearSceneBtn = document.getElementById('clearSceneBtn');
const fitViewBtn = document.getElementById('fitViewBtn');
const wireframeBtn = document.getElementById('wireframeBtn');
const expandAllBtn = document.getElementById('expandAllBtn');
const collapseAllBtn = document.getElementById('collapseAllBtn');
const canvas = document.getElementById('viewerCanvas');
const canvasWrap = document.getElementById('viewerCanvasWrap');
const treeLegend = document.getElementById('treeLegend');
const vehicleMatchStatus = document.getElementById('vehicleMatchStatus');
const vehicleOverrideSelect = document.getElementById('vehicleOverrideSelect');
const generateCheckpointsBtn = document.getElementById('generateCheckpointsBtn');
const checkpointsResults = document.getElementById('checkpointsResults');

// ---------- state ----------
let sourceMode = null;   // 'zip' | 'folder'
let zipBuffer = null;    // Uint8Array: 선택한 ZIP 전체 (zip 모드에서만 메모리에 보관)
let folderFileMap = null; // Map<filename, File> (folder 모드)
const loadedParts = new Map(); // filename -> { mesh, geometry, material, triCount }
let totalTriangles = 0;
let wireframeOn = false;

// 대분류(큰 부품 카테고리)별 색상 매핑 — 모델이 바뀌어도 hierarchy.json 구조만으로 자동 판단
let categoryNodeColorMap = new Map(); // 트리 노드 객체 참조 -> 색상(hex 정수)
let fileColorMap = new Map();         // STL 파일명 -> 색상(hex 정수)
const checkboxMeta = new WeakMap();    // checkbox -> { badge, originalCountText }

// 벤치마킹 체크포인트 기능용 상태 — 현재 로드된 트리 전체와 매칭된 teardown_data.json 차량
let currentTreeRoot = null;       // hierarchy.json 파싱 결과(루트, 배열일 수도 있음)
let currentTreeFiles = [];         // 현재 트리에 들어있는 전체 STL 파일명(체크 여부와 무관)
let teardownData = null;           // teardown_data.json 전체(첫 로드 후 캐시)
let matchedVehicleInfo = null;     // { vehicle, ratio, matched, total } | null

const LARGE_LOAD_TRIANGLE_WARNING = 4_000_000; // 이 이상 누적되면 추가 로드 전 확인창

// 카테고리에 일관된 색상을 부여하기 위한 팔레트 (Tailwind 계열 중간톤 채도)
const CATEGORY_PALETTE = [
    0x3b82f6, // blue
    0xef4444, // red
    0x22c55e, // green
    0xf59e0b, // amber
    0xa855f7, // purple
    0x06b6d4, // cyan
    0xec4899, // pink
    0x84cc16, // lime
    0xf97316, // orange
    0x14b8a6, // teal
    0x8b5cf6, // violet
    0xe11d48, // rose
];

// 이름 문자열을 해시로 변환해서 팔레트 인덱스로 바꾼다 — 같은 카테고리명은 언제나 같은 색상
function hashString(str) {
    let h = 0;
    for (let i = 0; i < str.length; i++) {
        h = (h * 31 + str.charCodeAt(i)) | 0;
    }
    return Math.abs(h);
}

function colorForName(name) {
    return CATEGORY_PALETTE[hashString(name || 'category') % CATEGORY_PALETTE.length];
}

function colorToCss(color) {
    return '#' + color.toString(16).padStart(6, '0');
}

// hierarchy.json은 모델마다 최상위 래핑 노드의 깊이가 다를 수 있어, 자식이 1개뿐인 "포장" 노드들을
// 건너뛰어 실제로 여러 갈래(큰 부품 카테고리)로 갈리는 지점을 찾는다. 이렇게 하면
// 차종마다 트리 깊이가 다르더라도 "큰 카테고리" 단위를 일관되게 찾을 수 있다.
function getCategoryNodes(rootNode) {
    const topArray = Array.isArray(rootNode) ? rootNode : [rootNode];
    if (topArray.length > 1) return topArray;
    let node = topArray[0];
    while (node && node.children && node.children.length === 1 && !node.files) {
        node = node.children[0];
    }
    if (node && node.children && node.children.length > 0) return node.children;
    return node ? [node] : [];
}

function buildCategoryColorMaps(rootNode) {
    const categories = getCategoryNodes(rootNode);
    const nodeColorMap = new Map();
    const fcm = new Map();
    categories.forEach((cat) => {
        const color = colorForName(cat.nodeName);
        nodeColorMap.set(cat, color);
        collectLeafFiles(cat).forEach((f) => {
            if (!fcm.has(f)) fcm.set(f, color);
        });
    });
    return { categories, nodeColorMap, fileColorMap: fcm };
}

function renderLegend(categories) {
    treeLegend.innerHTML = '';
    categories.forEach((cat) => {
        const color = categoryNodeColorMap.get(cat);
        const pill = document.createElement('span');
        pill.className = 'inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] bg-slate-50 border border-slate-200 text-slate-600';
        const swatch = document.createElement('span');
        swatch.className = 'w-2 h-2 rounded-full flex-shrink-0';
        swatch.style.backgroundColor = colorToCss(color);
        pill.appendChild(swatch);
        const text = document.createElement('span');
        text.textContent = cat.nodeName || '(이름없음)';
        pill.appendChild(text);
        treeLegend.appendChild(pill);
    });
}

// ---------- three.js 초기화 ----------
let scene, camera, renderer, controls;
let gridHelper = null;
let axisScene, axisCamera;

function initThree() {
    scene = new THREE.Scene();
    scene.background = new THREE.Color(0xf1f5f9);

    camera = new THREE.PerspectiveCamera(50, 1, 0.1, 500000);
    // STL 데이터의 실제 축 관례(Z축=높이/수직, X/Y=바닥면)에 맞춰, Z를 화면 수직축으로 설정한다.
    // 기본 시점: X축이 우측, Y축이 좌측으로 보이도록 (+X, -Y, +Z) 방향에서 바라본다.
    camera.up.set(0, 0, 1);
    camera.position.set(1500, -1500, 1200);

    renderer = new THREE.WebGLRenderer({ canvas, antialias: true });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    // 우측 상단 축 기즈모를 그릴 때 autoClear가 기본값(검정색)으로 덮어버려 검정 박스로
    // 보이는 문제를 막기 위해, 메인 씬 배경과 같은 색으로 clearColor를 맞춰둔다.
    renderer.setClearColor(0xf1f5f9, 1);

    controls = new OrbitControls(camera, renderer.domElement);
    controls.enableDamping = false; // 마우스를 놓으면 관성 없이 즉시 멈추도록 설정

    scene.add(new THREE.AmbientLight(0xffffff, 0.6));
    const dir1 = new THREE.DirectionalLight(0xffffff, 0.8);
    dir1.position.set(1, 1, 1);
    scene.add(dir1);
    const dir2 = new THREE.DirectionalLight(0xffffff, 0.4);
    dir2.position.set(-1, -0.5, -1);
    scene.add(dir2);

    updateGrid();
    initAxisGizmo();

    resizeRenderer();
    window.addEventListener('resize', resizeRenderer);
    requestAnimationFrame(animate);
}

// 로드된 파트의 전체 바운딩 박스에 맞춰 격자 plate 크기/중심을 다시 계산한다.
// 모델마다 좌표 범위가 다르므로, 파트를 로드/언로드할 때마다 다시 호출해 항상
// 현재 로드된 형상을 넉넉히 덮도록 유지한다(모델이 격자 밖으로 넘어가는 문제 방지).
function updateGrid() {
    if (gridHelper) {
        scene.remove(gridHelper);
        gridHelper.geometry.dispose();
        gridHelper.material.dispose();
        gridHelper = null;
    }

    let size = 6000;
    let divisions = 40;
    let center = new THREE.Vector3(0, 0, 0);
    let floorZ = 0;

    if (loadedParts.size) {
        const box = new THREE.Box3();
        for (const [, entry] of loadedParts) box.expandByObject(entry.mesh);
        const boxSize = box.getSize(new THREE.Vector3());
        center = box.getCenter(new THREE.Vector3());
        floorZ = box.min.z; // Z축이 수직(높이)이므로, 가장 낮은 Z가 "바닥(floor)"이 된다
        const footprint = Math.max(boxSize.x, boxSize.z, boxSize.y) || 1000;
        size = footprint * 2.2; // 여유 폭을 둬서 모델이 항상 격자 안쪽에 들어오도록 함
        divisions = Math.max(10, Math.round(size / 150));
    }

    gridHelper = new THREE.GridHelper(size, divisions, 0xcbd5e1, 0xe2e8f0);
    // GridHelper는 기본적으로 XZ 평면(Y-up 가정)에 그려지므로, X축 기준으로 90도 회전해
    // XY 평면(Z-up 바닥면)으로 맞춘다.
    gridHelper.rotation.x = Math.PI / 2;
    gridHelper.position.set(center.x, center.y, floorZ);
    scene.add(gridHelper);
}

// ---------- 우측 상단 X/Y/Z 축 방향 표시기 ----------
// 메인 카메라와 동일한 회전을 따라가는 작은 보조 씬을 만들어, 렌더링 시 뷰포트의
// 우측 상단 한쪽 구석에만 시저(scissor)로 잘라 그려 넣는다(별도 캔버스/추가 WebGL 컨텍스트 불필요).
function makeAxisLabelSprite(text, hexColor) {
    const res = 64;
    const c = document.createElement('canvas');
    c.width = res;
    c.height = res;
    const ctx = c.getContext('2d');
    ctx.fillStyle = hexColor;
    ctx.beginPath();
    ctx.arc(res / 2, res / 2, res / 2 - 2, 0, Math.PI * 2);
    ctx.fill();
    ctx.fillStyle = '#ffffff';
    ctx.font = 'bold 34px sans-serif';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillText(text, res / 2, res / 2 + 2);
    const texture = new THREE.CanvasTexture(c);
    const material = new THREE.SpriteMaterial({ map: texture, depthTest: false, depthWrite: false });
    const sprite = new THREE.Sprite(material);
    sprite.scale.set(0.55, 0.55, 0.55);
    return sprite;
}

function initAxisGizmo() {
    axisScene = new THREE.Scene();
    axisCamera = new THREE.PerspectiveCamera(40, 1, 0.1, 10);

    const AXIS_LEN = 1;
    const axes = [
        { dir: new THREE.Vector3(1, 0, 0), color: 0xef4444, label: 'X' },
        { dir: new THREE.Vector3(0, 1, 0), color: 0x22c55e, label: 'Y' },
        { dir: new THREE.Vector3(0, 0, 1), color: 0x3b82f6, label: 'Z' },
    ];
    axes.forEach(({ dir, color, label }) => {
        const arrow = new THREE.ArrowHelper(dir, new THREE.Vector3(0, 0, 0), AXIS_LEN, color, 0.3, 0.18);
        axisScene.add(arrow);
        const hex = '#' + color.toString(16).padStart(6, '0');
        const sprite = makeAxisLabelSprite(label, hex);
        sprite.position.copy(dir).multiplyScalar(AXIS_LEN + 0.4);
        sprite.scale.set(0.55, 0.55, 0.55);
        axisScene.add(sprite);
    });
}

function renderAxisGizmo() {
    const vpSize = 130;
    const margin = 10;
    const fullW = canvasWrap.clientWidth;
    const fullH = canvasWrap.clientHeight;
    if (!fullW || !fullH) return;

    const x = fullW - vpSize - margin;
    const y = fullH - vpSize - margin; // GL 좌표계는 좌하단이 원점이므로, 위쪽에 그리려면 이렇게 계산

    const camDir = new THREE.Vector3();
    camera.getWorldDirection(camDir);
    // 화살표+라벨이 작은 뷰포트 안에 확실히 들어오도록(스프라이트 자체 폭까지 감안해) 카메라를 충분히 멀리(5.5) 떨어뜨린다.
    axisCamera.position.copy(camDir).multiplyScalar(-5.5);
    axisCamera.up.copy(camera.up);
    axisCamera.lookAt(0, 0, 0);

    renderer.setViewport(x, y, vpSize, vpSize);
    renderer.setScissor(x, y, vpSize, vpSize);
    renderer.setScissorTest(true);
    renderer.clearDepth();
    renderer.render(axisScene, axisCamera);

    renderer.setViewport(0, 0, fullW, fullH);
    renderer.setScissorTest(false);
}

function resizeRenderer() {
    const w = canvasWrap.clientWidth || 1;
    const h = canvasWrap.clientHeight || 1;
    renderer.setSize(w, h, false);
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
}

function animate() {
    requestAnimationFrame(animate);
    controls.update();
    renderer.render(scene, camera);
    renderAxisGizmo();
}


// ---------- 로딩 배너 ----------
function setLoading(on, msg) {
    loadingBanner.classList.toggle('hidden', !on);
    if (msg) loadingBanner.textContent = `⏳ ${msg}`;
}

// ---------- ZIP에서 파일 하나만 추출 ----------
function unzipOne(buf, name) {
    return new Promise((resolve, reject) => {
        unzip(buf, { filter: (f) => f.name === name }, (err, data) => {
            if (err) return reject(err);
            resolve((data && data[name]) || null);
        });
    });
}

// ---------- 소스 선택 핸들러 ----------
zipInput.addEventListener('change', async (e) => {
    const file = e.target.files[0];
    if (!file) return;
    resetSource();
    sourceMode = 'zip';
    sourceStatus.textContent = `ZIP 로딩 중: ${file.name} (${(file.size / 1024 / 1024).toFixed(1)}MB)`;
    setLoading(true, 'ZIP 파일을 메모리로 읽는 중... (용량이 클 경우 시간이 걸릴 수 있습니다)');
    try {
        const buf = new Uint8Array(await file.arrayBuffer());
        const hierarchyBytes = await unzipOne(buf, 'hierarchy.json');
        if (!hierarchyBytes) {
            alert('선택한 ZIP 안에서 hierarchy.json을 찾지 못했습니다. A2MAC1 STL Pack 형식(ZIP 루트에 hierarchy.json + .stl 파일들)인지 확인해주세요.');
            setLoading(false);
            return;
        }
        zipBuffer = buf;
        const tree = JSON.parse(new TextDecoder().decode(hierarchyBytes));
        applyCategoryColors(tree);
        sourceStatus.textContent = `✅ ZIP 로드 완료: ${file.name}`;
        renderTree(tree);
        currentTreeRoot = tree;
        currentTreeFiles = collectAllFiles(tree);
        await autoMatchVehicle();
    } catch (err) {
        console.error(err);
        alert('ZIP 파일을 읽는 중 오류가 발생했습니다: ' + err.message);
        sourceStatus.textContent = '아직 선택된 파일이 없습니다.';
    } finally {
        setLoading(false);
    }
});

folderInput.addEventListener('change', async (e) => {
    const files = Array.from(e.target.files || []);
    if (!files.length) return;
    resetSource();
    sourceMode = 'folder';

    const map = new Map();
    let hierarchyFile = null;
    for (const f of files) {
        const base = (f.webkitRelativePath || f.name).split('/').pop();
        map.set(base, f);
        if (base === 'hierarchy.json') hierarchyFile = f;
    }
    if (!hierarchyFile) {
        alert('선택한 폴더 안에서 hierarchy.json을 찾지 못했습니다. A2MAC1 STL Pack 압축을 해제한 폴더가 맞는지 확인해주세요.');
        return;
    }
    setLoading(true, '폴더 구조를 읽는 중...');
    try {
        const text = await hierarchyFile.text();
        const tree = JSON.parse(text);
        folderFileMap = map;
        applyCategoryColors(tree);
        sourceStatus.textContent = `✅ 폴더 로드 완료 (${files.length}개 파일 인식됨)`;
        renderTree(tree);
        currentTreeRoot = tree;
        currentTreeFiles = collectAllFiles(tree);
        await autoMatchVehicle();
    } catch (err) {
        console.error(err);
        alert('hierarchy.json 파싱 중 오류: ' + err.message);
        sourceStatus.textContent = '아직 선택된 파일이 없습니다.';
    } finally {
        setLoading(false);
    }
});

function resetSource() {
    zipBuffer = null;
    folderFileMap = null;
    categoryNodeColorMap = new Map();
    fileColorMap = new Map();
    clearAllLoadedParts();
    treeRoot.innerHTML = '';
    treeLegend.innerHTML = '';

    currentTreeRoot = null;
    currentTreeFiles = [];
    matchedVehicleInfo = null;
    vehicleMatchStatus.textContent = 'ZIP 또는 폴더를 먼저 선택해주세요.';
    vehicleOverrideSelect.classList.add('hidden');
    vehicleOverrideSelect.innerHTML = '';
    generateCheckpointsBtn.disabled = true;
    checkpointsResults.innerHTML = '<p class="text-slate-400 text-[11px] p-2">아직 생성된 체크포인트가 없습니다.</p>';
}

function applyCategoryColors(tree) {
    const { categories, nodeColorMap, fileColorMap: fcm } = buildCategoryColorMaps(tree);
    categoryNodeColorMap = nodeColorMap;
    fileColorMap = fcm;
    renderLegend(categories);
}

// ---------- 활성 소스에서 파일 바이트 가져오기 ----------
async function getFileBytes(filename) {
    if (sourceMode === 'zip') {
        if (!zipBuffer) throw new Error('ZIP이 아직 로드되지 않았습니다.');
        const bytes = await unzipOne(zipBuffer, filename);
        if (!bytes) throw new Error(`ZIP 안에서 ${filename} 파일을 찾을 수 없습니다.`);
        return bytes;
    }
    if (sourceMode === 'folder') {
        const file = folderFileMap && folderFileMap.get(filename);
        if (!file) throw new Error(`폴더 안에서 ${filename} 파일을 찾을 수 없습니다.`);
        return new Uint8Array(await file.arrayBuffer());
    }
    throw new Error('선택된 소스가 없습니다.');
}

// ---------- 트리 렌더링 ----------
function collectLeafFiles(node, out = []) {
    if (node.files) out.push(...node.files);
    if (node.children) node.children.forEach((c) => collectLeafFiles(c, out));
    return out;
}

// 루트가 배열일 수도(여러 최상위 노드) 있는 형태를 통일해서, 트리 전체의 STL 파일명을 모은다
function collectAllFiles(rootNode) {
    const arr = Array.isArray(rootNode) ? rootNode : [rootNode];
    const out = [];
    arr.forEach((n) => collectLeafFiles(n, out));
    return out;
}

function renderTree(rootNode) {
    treeRoot.innerHTML = '';
    const rootArray = Array.isArray(rootNode) ? rootNode : [rootNode];
    rootArray.forEach((n) => treeRoot.appendChild(buildNodeEl(n, null)));
}

let nodeCounter = 0;
function buildNodeEl(node, inheritedColor) {
    const id = `node-${nodeCounter++}`;
    // 이 노드가 카테고리 기준점이면 새 색을 부여하고, 아니면 부모에게 물려받은 색을 그대로 이어받는다
    const color = categoryNodeColorMap.has(node) ? categoryNodeColorMap.get(node) : inheritedColor;

    const wrap = document.createElement('div');
    wrap.className = 'tree-node';

    const row = document.createElement('div');
    row.className = 'tree-node-row flex items-center gap-1.5 py-0.5 px-1 rounded';
    if (color != null) {
        row.style.borderLeft = `3px solid ${colorToCss(color)}`;
    }

    const hasChildren = !!(node.children && node.children.length);
    const leafFiles = collectLeafFiles(node);

    let toggleBtn = null;
    if (hasChildren) {
        toggleBtn = document.createElement('button');
        toggleBtn.type = 'button';
        toggleBtn.textContent = '▾';
        toggleBtn.className = 'w-4 text-slate-400 text-[10px] flex-shrink-0';
        row.appendChild(toggleBtn);
    } else {
        const spacer = document.createElement('span');
        spacer.className = 'w-4 flex-shrink-0';
        row.appendChild(spacer);
    }

    const checkbox = document.createElement('input');
    checkbox.type = 'checkbox';
    checkbox.className = 'flex-shrink-0';
    checkbox.id = id;
    row.appendChild(checkbox);

    if (color != null) {
        const swatch = document.createElement('span');
        swatch.className = 'w-2 h-2 rounded-full flex-shrink-0';
        swatch.style.backgroundColor = colorToCss(color);
        row.appendChild(swatch);
    }

    const label = document.createElement('label');
    label.htmlFor = id;
    label.className = 'flex-1 truncate select-none cursor-pointer';
    label.textContent = node.nodeName || '(이름없음)';
    label.title = `${node.nodeName || ''} — 파일 ${leafFiles.length}개`;
    row.appendChild(label);

    const countBadge = document.createElement('span');
    const originalCountText = leafFiles.length ? String(leafFiles.length) : '';
    countBadge.className = 'text-[10px] text-slate-400 flex-shrink-0';
    countBadge.textContent = originalCountText;
    row.appendChild(countBadge);

    checkboxMeta.set(checkbox, { badge: countBadge, originalCountText });

    wrap.appendChild(row);

    let childrenWrap = null;
    if (hasChildren) {
        childrenWrap = document.createElement('div');
        childrenWrap.className = 'tree-children';
        node.children.forEach((c) => childrenWrap.appendChild(buildNodeEl(c, color)));
        wrap.appendChild(childrenWrap);

        toggleBtn.addEventListener('click', () => {
            const collapsed = childrenWrap.classList.toggle('hidden');
            toggleBtn.textContent = collapsed ? '▸' : '▾';
        });
    }

    checkbox.addEventListener('change', async () => {
        checkbox.disabled = true;
        // 상위 부품 체크 시 하위 부품 체크박스도 함께 체크/해제되도록 동기화
        const descendantCheckboxes = childrenWrap
            ? Array.from(childrenWrap.querySelectorAll('input[type=checkbox]'))
            : [];
        try {
            if (checkbox.checked) {
                await loadFilesForNode(leafFiles, countBadge);
                descendantCheckboxes.forEach((cb) => {
                    cb.checked = true;
                    const meta = checkboxMeta.get(cb);
                    if (meta) {
                        meta.badge.textContent = '✔';
                        meta.badge.classList.add('text-emerald-600', 'font-semibold');
                    }
                });
            } else {
                unloadFiles(leafFiles);
                countBadge.textContent = originalCountText;
                countBadge.classList.remove('text-emerald-600', 'font-semibold');
                descendantCheckboxes.forEach((cb) => {
                    cb.checked = false;
                    const meta = checkboxMeta.get(cb);
                    if (meta) {
                        meta.badge.textContent = meta.originalCountText;
                        meta.badge.classList.remove('text-emerald-600', 'font-semibold');
                    }
                });
            }
        } catch (err) {
            console.error(err);
            alert('로드 중 오류: ' + err.message);
            checkbox.checked = false;
        } finally {
            checkbox.disabled = false;
        }
    });

    return wrap;
}

// ---------- 파트 로드 / 언로드 ----------
async function loadFilesForNode(filenames, countBadge) {
    const toLoad = filenames.filter((f) => !loadedParts.has(f));
    if (!toLoad.length) return;

    if (totalTriangles > LARGE_LOAD_TRIANGLE_WARNING) {
        const proceed = confirm(
            `현재 이미 ${Math.round(totalTriangles).toLocaleString()}개의 삼각형이 로드되어 있습니다. 더 불러오면 브라우저가 느려질 수 있습니다. 계속할까요?`
        );
        if (!proceed) return;
    }

    const loader = new STLLoader();
    for (let i = 0; i < toLoad.length; i++) {
        const filename = toLoad[i];
        setLoading(true, `(${i + 1}/${toLoad.length}) ${filename} 로드 중...`);
        const bytes = await getFileBytes(filename);
        const arrayBuffer = bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength);
        const geometry = loader.parse(arrayBuffer);
        geometry.computeVertexNormals();
        geometry.computeBoundingBox();

        const material = new THREE.MeshStandardMaterial({
            color: fileColorMap.get(filename) ?? 0x64748b,
            metalness: 0.15,
            roughness: 0.7,
            wireframe: wireframeOn,
            side: THREE.DoubleSide,
        });
        const mesh = new THREE.Mesh(geometry, material);
        scene.add(mesh);

        const triCount = geometry.index ? geometry.index.count / 3 : geometry.attributes.position.count / 3;
        loadedParts.set(filename, { mesh, geometry, material, triCount });
        totalTriangles += triCount;
    }

    if (countBadge) {
        countBadge.textContent = '✔';
        countBadge.classList.add('text-emerald-600', 'font-semibold');
    }
    updateGrid();
    updateStats();
    setLoading(false);
}

function unloadFiles(filenames) {
    for (const filename of filenames) {
        const entry = loadedParts.get(filename);
        if (!entry) continue;
        scene.remove(entry.mesh);
        entry.geometry.dispose();
        entry.material.dispose();
        totalTriangles -= entry.triCount;
        loadedParts.delete(filename);
    }
    updateGrid();
    updateStats();
}

function clearAllLoadedParts() {
    for (const [, entry] of loadedParts) {
        scene.remove(entry.mesh);
        entry.geometry.dispose();
        entry.material.dispose();
    }
    loadedParts.clear();
    totalTriangles = 0;
    updateGrid();
    updateStats();
    treeRoot.querySelectorAll('input[type=checkbox]').forEach((cb) => {
        cb.checked = false;
        const meta = checkboxMeta.get(cb);
        if (meta) {
            meta.badge.textContent = meta.originalCountText;
            meta.badge.classList.remove('text-emerald-600', 'font-semibold');
        }
    });
}

function updateStats() {
    statParts.textContent = loadedParts.size.toLocaleString();
    statTris.textContent = Math.round(totalTriangles).toLocaleString();
}

// ---------- 뷰어 툴바 ----------
clearSceneBtn.addEventListener('click', () => {
    if (!loadedParts.size) return;
    if (confirm('로드된 모든 파트를 비우시겠습니까?')) clearAllLoadedParts();
});

fitViewBtn.addEventListener('click', () => {
    if (!loadedParts.size) return;
    const box = new THREE.Box3();
    for (const [, entry] of loadedParts) box.expandByObject(entry.mesh);
    const center = box.getCenter(new THREE.Vector3());
    const size = box.getSize(new THREE.Vector3());
    const maxDim = Math.max(size.x, size.y, size.z) || 1000;
    const dist = maxDim * 1.6;
    // Z축이 수직(up)이므로, 기본 카메라 시점(+X, -Y, +Z 쪽)과 같은 방향으로 맞춰 이동한다.
    camera.position.set(center.x + dist, center.y - dist, center.z + dist * 0.7);
    camera.near = Math.max(maxDim / 1000, 0.1);
    camera.far = maxDim * 50;
    camera.updateProjectionMatrix();
    controls.target.copy(center);
    controls.update();
});

wireframeBtn.addEventListener('click', () => {
    wireframeOn = !wireframeOn;
    wireframeBtn.classList.toggle('bg-slate-800', wireframeOn);
    wireframeBtn.classList.toggle('text-white', wireframeOn);
    for (const [, entry] of loadedParts) entry.material.wireframe = wireframeOn;
});

expandAllBtn.addEventListener('click', () => {
    treeRoot.querySelectorAll('.tree-children').forEach((el) => el.classList.remove('hidden'));
    treeRoot.querySelectorAll('.tree-node-row > button').forEach((b) => (b.textContent = '▾'));
});
collapseAllBtn.addEventListener('click', () => {
    treeRoot.querySelectorAll('.tree-children').forEach((el) => el.classList.add('hidden'));
    treeRoot.querySelectorAll('.tree-node-row > button').forEach((b) => (b.textContent = '▸'));
});

// ---------- 구조적 벤치마킹 체크포인트 ----------
// STL 파일명(IB_<partCode>_<이름>.stl)과 teardown_data.json의 bomTree[].partCode가 같은
// 코드 체계를 쓰는 것을 이용해, 로드된 STL 트리와 가장 많이 겹치는 차량을 자동으로 찾는다.
function parsePartCode(filename) {
    const m = /^IB_([0-9]+-[0-9]+-[0-9]+)_/.exec(filename);
    return m ? m[1] : null;
}

async function ensureTeardownDataLoaded() {
    if (teardownData) return teardownData;
    try {
        const res = await fetch('./teardown_data.json', { cache: 'no-store' });
        if (!res.ok) throw new Error('HTTP ' + res.status);
        teardownData = await res.json();
    } catch (err) {
        console.error('teardown_data.json 로드 실패', err);
        teardownData = { vehicles: [] };
    }
    return teardownData;
}

function populateVehicleDropdown(vehicles) {
    vehicleOverrideSelect.innerHTML = '';
    vehicles
        .slice()
        .sort((a, b) => (a.name || '').localeCompare(b.name || ''))
        .forEach((v) => {
            const opt = document.createElement('option');
            opt.value = v.id;
            opt.textContent = `${v.name || v.id}${v.year ? ' (' + v.year + ')' : ''}`;
            vehicleOverrideSelect.appendChild(opt);
        });
    vehicleOverrideSelect.classList.remove('hidden');
}

function setMatchedVehicle(vehicle, matchInfo) {
    matchedVehicleInfo = vehicle ? { vehicle, ratio: matchInfo?.ratio ?? null, matched: matchInfo?.matched ?? null, total: matchInfo?.total ?? null } : null;
    if (vehicle) {
        vehicleOverrideSelect.value = vehicle.id;
        const pctTxt = matchInfo && matchInfo.total
            ? ` (매칭률 ${Math.round(matchInfo.ratio * 100)}%, partCode ${matchInfo.matched}/${matchInfo.total}개 일치)`
            : '';
        vehicleMatchStatus.innerHTML = `자동 추천된 차량: <b class="text-sky-700">${vehicle.name || vehicle.id}</b>${pctTxt} <span class="text-slate-400">(다른 차량이면 아래에서 변경)</span>`;
    } else {
        vehicleMatchStatus.textContent = '자동 매칭에 실패했습니다. 아래 목록에서 직접 차량을 선택해주세요.';
    }
    generateCheckpointsBtn.disabled = !vehicle;
}

async function autoMatchVehicle() {
    vehicleMatchStatus.textContent = '차량 자동 매칭 중...';
    generateCheckpointsBtn.disabled = true;
    const data = await ensureTeardownDataLoaded();
    const vehicles = data.vehicles || [];
    if (!vehicles.length) {
        vehicleMatchStatus.textContent = 'teardown_data.json을 불러오지 못했습니다(assembly-viewer.html과 같은 폴더에 있는지 확인해주세요).';
        return;
    }
    populateVehicleDropdown(vehicles);

    const stlCodes = new Set();
    currentTreeFiles.forEach((f) => {
        const code = parsePartCode(f);
        if (code) stlCodes.add(code);
    });

    let best = null;
    vehicles.forEach((v) => {
        const bomCodes = new Set((v.bomTree || []).map((n) => n.partCode).filter(Boolean));
        if (!bomCodes.size) return;
        let matched = 0;
        bomCodes.forEach((c) => {
            if (stlCodes.has(c)) matched++;
        });
        const ratio = matched / bomCodes.size;
        if (!best || ratio > best.ratio) best = { vehicle: v, ratio, matched, total: bomCodes.size };
    });

    setMatchedVehicle(best ? best.vehicle : null, best);
}

vehicleOverrideSelect.addEventListener('change', () => {
    const vehicles = (teardownData && teardownData.vehicles) || [];
    const vehicle = vehicles.find((v) => v.id === vehicleOverrideSelect.value);
    if (!vehicle) return;
    matchedVehicleInfo = { vehicle, ratio: null, matched: null, total: null };
    vehicleMatchStatus.innerHTML = `수동 선택된 차량: <b class="text-sky-700">${vehicle.name || vehicle.id}</b>`;
    generateCheckpointsBtn.disabled = false;
});

function getNum(obj, key) {
    const v = obj ? obj[key] : undefined;
    return typeof v === 'number' && !Number.isNaN(v) ? v : null;
}

// modules[] 항목은 "Module Weight [kg]"/"Module -2 Weight [kg]"처럼 모듈 종류별로 키 접미사가 붙으므로,
// 패턴 매칭으로 "무게×개수"를 일반화해서 합산한다.
function sumModuleWeights(modules) {
    let total = 0;
    (modules || []).forEach((m) => {
        const weightKey = Object.keys(m).find((k) => /Module.*Weight \[kg\]/i.test(k));
        const countKey = Object.keys(m).find((k) => /Number of modules/i.test(k));
        const w = weightKey ? m[weightKey] : null;
        const c = countKey ? m[countKey] : null;
        if (typeof w === 'number' && typeof c === 'number') total += w * c;
    });
    return total;
}

function mkRow(category, label, value, unit, evidence) {
    return { category, label, value, unit, evidence };
}

// hierarchy.json의 "리프 노드"(files를 직접 가진 노드) 중 이름이 패턴에 맞는 것들의 파일 총개수
function countFilesByNamePattern(rootNode, pattern) {
    let count = 0;
    function walk(node) {
        if (node.files && pattern.test(node.nodeName || '')) count += node.files.length;
        if (node.children) node.children.forEach(walk);
    }
    (Array.isArray(rootNode) ? rootNode : [rootNode]).forEach(walk);
    return count;
}

function findNodesByNamePattern(rootNode, pattern) {
    const out = [];
    function walk(node) {
        if (pattern.test(node.nodeName || '')) out.push(node);
        if (node.children) node.children.forEach(walk);
    }
    (Array.isArray(rootNode) ? rootNode : [rootNode]).forEach(walk);
    return out;
}

// 체크포인트 계산 전용 — 뷰어 장면에는 추가하지 않고 bbox만 구한 뒤 지오메트리를 즉시 해제한다.
async function getStlBBox(filename) {
    const bytes = await getFileBytes(filename);
    const arrayBuffer = bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength);
    const geometry = new STLLoader().parse(arrayBuffer);
    geometry.computeBoundingBox();
    const box = geometry.boundingBox;
    const size = new THREE.Vector3();
    box.getSize(size);
    const result = { min: box.min.clone(), max: box.max.clone(), size };
    geometry.dispose();
    return result;
}

// 셀 피치(인접 셀 간 간격): 가장 많은 인스턴스를 가진 "Cell" 리프 노드에서 샘플 2개만 읽어
// 중심좌표 차이가 가장 큰 축을 "적층 방향"으로 보고 그 축의 간격을 피치로 추정한다.
async function computeCellPitchCheckpoint(tree) {
    const cellNodes = findNodesByNamePattern(tree, /^Cell$/i).filter((n) => n.files && n.files.length >= 2);
    if (!cellNodes.length) return null;
    const best = cellNodes.reduce((a, b) => (b.files.length > a.files.length ? b : a));
    const [b1, b2] = await Promise.all([getStlBBox(best.files[0]), getStlBBox(best.files[1])]);
    const c1 = b1.min.clone().add(b1.max).multiplyScalar(0.5);
    const c2 = b2.min.clone().add(b2.max).multiplyScalar(0.5);
    const d = { x: Math.abs(c1.x - c2.x), y: Math.abs(c1.y - c2.y), z: Math.abs(c1.z - c2.z) };
    let axis = 'x';
    if (d.y > d[axis]) axis = 'y';
    if (d.z > d[axis]) axis = 'z';
    return { pitch: d[axis], axis, fileCount: best.files.length, sample: [best.files[0], best.files[1]] };
}

// Cooling Plate 커버리지: 대표 샘플 파일 1개씩만 읽어 X-Y 평면 footprint 면적비를 근사한다.
async function computeCoolingPlateCoverage(tree) {
    const plateNodes = findNodesByNamePattern(tree, /cooling.?plate/i).filter((n) => n.files && n.files.length);
    const moduleNodes = findNodesByNamePattern(tree, /cell.?module/i).filter((n) => n.files && n.files.length);
    if (!plateNodes.length || !moduleNodes.length) return null;
    const plateFile = plateNodes[0].files[0];
    const moduleFile = moduleNodes[0].files[0];
    const [plateBox, moduleBox] = await Promise.all([getStlBBox(plateFile), getStlBBox(moduleFile)]);
    const plateArea = plateBox.size.x * plateBox.size.y;
    const moduleArea = moduleBox.size.x * moduleBox.size.y;
    if (!moduleArea) return null;
    return { pct: (plateArea / moduleArea) * 100, plateArea, moduleArea, plateFile, moduleFile };
}

async function computeAllCheckpoints(vehicle, tree) {
    const rows = [];
    const pack = vehicle.pack || {};
    const packOverview = vehicle.packOverview || {};
    const cell = vehicle.cell || {};
    const modules = vehicle.modules || [];

    // A. teardown_data.json 스펙 기반
    const cellVol = getNum(cell, 'Cell 3D Volume [L]');
    const cellCount = getNum(pack, 'Cells in Battery Pack');
    const packBoxVol = getNum(pack, 'Battery Pack Box Volume [L]');
    if (cellVol != null && cellCount != null && packBoxVol) {
        rows.push(mkRow('① 패키징 효율', '체적 패키징 효율 (셀 총부피 ÷ Pack Box 부피)', (cellVol * cellCount / packBoxVol) * 100, '%',
            `Cell 3D Volume ${cellVol}L × ${cellCount}개 ÷ Pack Box Volume ${packBoxVol}L (teardown_data.json)`));
    }

    const totalModuleWeight = sumModuleWeights(modules);
    const packWeight = getNum(pack, 'Battery Pack Weight [kg] ') ?? getNum(pack, 'Battery Pack Weight [kg]');
    if (totalModuleWeight && packWeight) {
        rows.push(mkRow('① 패키징 효율', '중량 효율 (모듈 총중량 ÷ Pack 총중량)', (totalModuleWeight / packWeight) * 100, '%',
            `모듈 총중량 ${totalModuleWeight.toFixed(1)}kg ÷ Pack 총중량 ${packWeight}kg (teardown_data.json)`));
    }

    const fastenerCount = getNum(packOverview, 'Number of Fasteners');
    const packLen = getNum(pack, 'Battery Pack Length [mm]');
    const packWidth = getNum(pack, 'Battery Pack Width [mm]');
    if (fastenerCount != null && packLen && packWidth) {
        const areaM2 = (packLen / 1000) * (packWidth / 1000);
        rows.push(mkRow('② 조립 복잡도', '체결구 밀도 (체결구 수 ÷ 바닥면적)', fastenerCount / areaM2, '개/㎡',
            `체결구 ${fastenerCount}개 ÷ (${packLen}×${packWidth}mm 바닥면적 ${areaM2.toFixed(2)}㎡) (teardown_data.json)`));
    }

    const partCount = getNum(packOverview, 'Number of Parts');
    if (partCount != null && packBoxVol) {
        rows.push(mkRow('② 조립 복잡도', '파트 수 밀도 (파트 수 ÷ Pack Box 부피)', partCount / packBoxVol, '개/L',
            `파트 수 ${partCount}개 ÷ Pack Box Volume ${packBoxVol}L (teardown_data.json)`));
    }

    // B. hierarchy.json 트리 구조 기반 (압축해제 불필요)
    if (currentTreeFiles.length) {
        const uniqueCodes = new Set(currentTreeFiles.map(parsePartCode).filter(Boolean));
        rows.push(mkRow('② 조립 복잡도', '부품 재사용률 (고유 파트 수 ÷ 전체 STL 인스턴스 수)', (uniqueCodes.size / currentTreeFiles.length) * 100, '%',
            `고유 partCode ${uniqueCodes.size}개 ÷ STL 인스턴스 ${currentTreeFiles.length}개 (hierarchy.json)`));
    }

    rows.push(mkRow('③ 구조 설계', '구조 보강재 개수 (Bracket/Support)', countFilesByNamePattern(tree, /bracket|support/i), '개',
        'hierarchy.json 노드명에 Bracket 또는 Support가 포함된 리프 파일 수'));
    rows.push(mkRow('③ 구조 설계', '실(Seal) 개수', countFilesByNamePattern(tree, /seal/i), '개',
        'hierarchy.json 노드명에 Seal이 포함된 리프 파일 수'));
    rows.push(mkRow('④ 전기 아키텍처', '전기 인터커넥트 부품 개수 (Bus Bar/HV Connector/Relay/Fuse)', countFilesByNamePattern(tree, /bus.?bar|hv.?connector|relay|fuse/i), '개',
        'hierarchy.json 노드명 패턴 매칭 리프 파일 수'));

    // C. 타겟 STL 형상 기반 (필요한 파일 몇 개만 온디맨드로 압축 해제)
    try {
        const pitchInfo = await computeCellPitchCheckpoint(tree);
        if (pitchInfo) {
            rows.push(mkRow('⑤ 셀/모듈 구성', `셀 피치 (인접 셀 간격, ${pitchInfo.axis.toUpperCase()}축 기준)`, pitchInfo.pitch, 'mm',
                `${pitchInfo.sample[0]} / ${pitchInfo.sample[1]} 두 인스턴스 좌표차 실측 (총 ${pitchInfo.fileCount}개 인스턴스 중 샘플 2개, STL)`));
        }
    } catch (err) {
        console.warn('셀 피치 계산 실패', err);
    }

    try {
        const coverage = await computeCoolingPlateCoverage(tree);
        if (coverage) {
            rows.push(mkRow('⑥ 열관리', 'Cooling Plate 커버리지 (plate 면적 ÷ 셀모듈 바닥면적)', coverage.pct, '%',
                `${coverage.plateFile} 면적 ${coverage.plateArea.toFixed(0)}㎟ ÷ ${coverage.moduleFile} 바닥면적 ${coverage.moduleArea.toFixed(0)}㎟ (STL 실측, 대표 샘플 기준 근사치)`));
        }
    } catch (err) {
        console.warn('냉각판 커버리지 계산 실패', err);
    }

    return rows;
}

function formatNum(v) {
    if (v == null || Number.isNaN(v)) return '-';
    return Math.abs(v) >= 100 ? v.toFixed(0) : v.toFixed(2);
}

function renderCheckpointResults(rows) {
    checkpointsResults.innerHTML = '';
    if (!rows.length) {
        checkpointsResults.innerHTML = '<p class="text-slate-400 text-[11px] p-2">계산 가능한 체크포인트가 없습니다(매칭된 차량의 데이터 필드 부족 또는 STL에서 해당 부품을 찾지 못했습니다).</p>';
        return;
    }
    const table = document.createElement('table');
    table.className = 'w-full text-xs border-collapse';
    table.innerHTML = `
        <thead>
            <tr class="text-left text-slate-500 border-b border-slate-200">
                <th class="py-1.5 pr-2 font-semibold whitespace-nowrap">영역</th>
                <th class="py-1.5 pr-2 font-semibold">체크포인트</th>
                <th class="py-1.5 pr-2 font-semibold">실측값(근거)</th>
                <th class="py-1.5 pr-2 font-semibold whitespace-nowrap">자사 제품 값(임시 입력)</th>
                <th class="py-1.5 pr-2 font-semibold whitespace-nowrap">차이</th>
            </tr>
        </thead>
    `;
    const tbody = document.createElement('tbody');
    rows.forEach((row) => {
        const tr = document.createElement('tr');
        tr.className = 'border-b border-slate-100 align-top';

        const tdCat = document.createElement('td');
        tdCat.className = 'py-2 pr-2 text-slate-400 whitespace-nowrap';
        tdCat.textContent = row.category;
        tr.appendChild(tdCat);

        const tdLabel = document.createElement('td');
        tdLabel.className = 'py-2 pr-2 font-semibold text-slate-700';
        tdLabel.textContent = row.label;
        tr.appendChild(tdLabel);

        const tdValue = document.createElement('td');
        tdValue.className = 'py-2 pr-2';
        tdValue.innerHTML = `<div class="font-bold text-slate-900">${formatNum(row.value)} ${row.unit}</div><div class="text-[10px] text-slate-400 mt-0.5">${row.evidence}</div>`;
        tr.appendChild(tdValue);

        const tdInput = document.createElement('td');
        tdInput.className = 'py-2 pr-2';
        const input = document.createElement('input');
        input.type = 'number';
        input.step = 'any';
        input.placeholder = `자사 값 (${row.unit})`;
        input.className = 'w-28 text-xs border border-slate-200 rounded px-1.5 py-1';
        tdInput.appendChild(input);
        tr.appendChild(tdInput);

        const tdDiff = document.createElement('td');
        tdDiff.className = 'py-2 pr-2 font-semibold text-slate-400';
        tdDiff.textContent = '—';
        tr.appendChild(tdDiff);

        input.addEventListener('input', () => {
            const companyVal = parseFloat(input.value);
            if (Number.isNaN(companyVal) || !row.value) {
                tdDiff.textContent = '—';
                tdDiff.className = 'py-2 pr-2 font-semibold text-slate-400';
                return;
            }
            const diffPct = ((companyVal - row.value) / row.value) * 100;
            const sign = diffPct > 0 ? '+' : '';
            tdDiff.textContent = `${sign}${diffPct.toFixed(1)}%`;
            tdDiff.className = `py-2 pr-2 font-semibold ${diffPct > 0 ? 'text-rose-600' : diffPct < 0 ? 'text-sky-600' : 'text-slate-500'}`;
        });

        tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    checkpointsResults.appendChild(table);
}

generateCheckpointsBtn.addEventListener('click', async () => {
    if (!matchedVehicleInfo || !matchedVehicleInfo.vehicle || !currentTreeRoot) return;
    const originalLabel = generateCheckpointsBtn.textContent;
    generateCheckpointsBtn.disabled = true;
    generateCheckpointsBtn.textContent = '⏳ 계산 중...';
    try {
        const rows = await computeAllCheckpoints(matchedVehicleInfo.vehicle, currentTreeRoot);
        renderCheckpointResults(rows);
    } catch (err) {
        console.error(err);
        alert('체크포인트 계산 중 오류: ' + err.message);
    } finally {
        generateCheckpointsBtn.disabled = false;
        generateCheckpointsBtn.textContent = originalLabel;
    }
});

initThree();
