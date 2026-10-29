// Image helpers: decode -> downscale -> JPEG. Uses createImageBitmap + canvas
// only (no blob:/data: URLs are rendered, so it works under a strict iframe CSP).

export const MAX_IMAGES = 4;
const VISION_MAX_EDGE = 768;
const VISION_QUALITY = 0.72;
const STORE_MAX_EDGE = 420;
const STORE_QUALITY = 0.65;

async function decode(file) {
  if (typeof createImageBitmap === "function") {
    try { return await createImageBitmap(file, { imageOrientation: "from-image" }); }
    catch { return await createImageBitmap(file); }
  }
  throw new Error("This browser cannot decode images (createImageBitmap missing).");
}

function drawResized(bmp, maxEdge) {
  const scale = Math.min(1, maxEdge / Math.max(bmp.width, bmp.height));
  const w = Math.max(1, Math.round(bmp.width * scale));
  const h = Math.max(1, Math.round(bmp.height * scale));
  const canvas = document.createElement("canvas");
  canvas.width = w; canvas.height = h;
  const ctx = canvas.getContext("2d");
  ctx.fillStyle = "#fff"; ctx.fillRect(0, 0, w, h);
  ctx.drawImage(bmp, 0, 0, w, h);
  return canvas;
}

/**
 * Returns:
 *   canvas       - preview canvas
 *   blob, size   - full-res compressed JPEG for APS files.* upload
 *   base64       - full-res compressed JPEG base64 for vision LLM
 *   thumbBase64  - compact JPEG base64 (~15-25 KB) for legacy KV storage fallback
 *   thumbSize    - byte length of the compact thumbnail
 *   mimeType, name
 */
export async function prepareImage(file) {
  if (!file.type.startsWith("image/")) throw new Error(`${file.name} is not an image`);
  const bmp = await decode(file);
  const canvas = drawResized(bmp, VISION_MAX_EDGE);
  const thumbCanvas = drawResized(bmp, STORE_MAX_EDGE);
  bmp.close?.();
  const blob = await new Promise((res, rej) =>
    canvas.toBlob((b) => (b ? res(b) : rej(new Error("JPEG encode failed"))), "image/jpeg", VISION_QUALITY));
  const dataUrl = canvas.toDataURL("image/jpeg", VISION_QUALITY);
  const thumbDataUrl = thumbCanvas.toDataURL("image/jpeg", STORE_QUALITY);
  const thumbBase64 = thumbDataUrl.split(",")[1];
  const thumbSize = Math.round((thumbBase64.length * 3) / 4);
  return {
    canvas,
    blob,
    base64: dataUrl.split(",")[1],
    thumbBase64,
    thumbSize,
    mimeType: "image/jpeg",
    size: blob.size,
    name: file.name,
  };
}

export function formatBytes(n) {
  if (!n) return "0 B";
  const u = ["B", "KB", "MB", "GB"]; let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(i ? 1 : 0)} ${u[i]}`;
}

