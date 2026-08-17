/** Renders the extension icons as PNGs. 4x supersampled for smooth edges. */
const fs = require("fs");
const zlib = require("zlib");

const OUT = "C:/PROJECTS/job auto fill/icons/";
const BG = [79, 70, 229];       // indigo, matches popup accent
const CARD = [255, 255, 255];
const LINE = [79, 70, 229];
const TICK = [34, 197, 94];     // green "learned" check

function crc32(buf) {
  let c, table = [];
  for (let n = 0; n < 256; n++) {
    c = n;
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    table[n] = c >>> 0;
  }
  let crc = 0xffffffff;
  for (let i = 0; i < buf.length; i++) crc = table[(crc ^ buf[i]) & 0xff] ^ (crc >>> 8);
  return (crc ^ 0xffffffff) >>> 0;
}

function chunk(type, data) {
  const len = Buffer.alloc(4);
  len.writeUInt32BE(data.length);
  const body = Buffer.concat([Buffer.from(type, "ascii"), data]);
  const crc = Buffer.alloc(4);
  crc.writeUInt32BE(crc32(body));
  return Buffer.concat([len, body, crc]);
}

function png(size, pixels) {
  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(size, 0);
  ihdr.writeUInt32BE(size, 4);
  ihdr[8] = 8;   // bit depth
  ihdr[9] = 6;   // RGBA
  const raw = Buffer.alloc(size * (size * 4 + 1));
  let o = 0;
  for (let y = 0; y < size; y++) {
    raw[o++] = 0; // filter: none
    for (let x = 0; x < size; x++) {
      const p = pixels[y * size + x];
      raw[o++] = p[0]; raw[o++] = p[1]; raw[o++] = p[2]; raw[o++] = p[3];
    }
  }
  return Buffer.concat([
    Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]),
    chunk("IHDR", ihdr),
    chunk("IDAT", zlib.deflateSync(raw, { level: 9 })),
    chunk("IEND", Buffer.alloc(0))
  ]);
}

/**
 * Shape sampled in unit space (0..1) -> [r,g,b,a] or null.
 * At 16px the check mark turns to mush, so the small variant drops it and
 * uses a wider card with fatter lines.
 */
function sample(u, v, small) {
  if (small) {
    const r = 0.18;
    const dx = Math.max(r - u, 0, u - (1 - r));
    const dy = Math.max(r - v, 0, v - (1 - r));
    if (Math.hypot(dx, dy) > r) return null;
    const x0 = 0.20, x1 = 0.80, y0 = 0.16, y1 = 0.84;
    if (u > x0 && u < x1 && v > y0 && v < y1) {
      const lines = [0.32, 0.50, 0.68];
      for (let i = 0; i < lines.length; i++) {
        const w = i === 2 ? 0.24 : 0.44;
        if (Math.abs(v - lines[i]) < 0.075 && u > x0 + 0.08 && u < x0 + 0.08 + w) {
          return LINE.concat(255);
        }
      }
      return CARD.concat(255);
    }
    return BG.concat(255);
  }
  // rounded-square background
  const r = 0.18;
  const inBg = (() => {
    const dx = Math.max(r - u, 0, u - (1 - r));
    const dy = Math.max(r - v, 0, v - (1 - r));
    return Math.hypot(dx, dy) <= r;
  })();
  if (!inBg) return null;

  // white card
  const cx0 = 0.24, cx1 = 0.72, cy0 = 0.18, cy1 = 0.82;
  const inCard = u > cx0 && u < cx1 && v > cy0 && v < cy1;

  // green check, bottom-right
  const tick = (() => {
    const x = (u - 0.60) / 0.34, y = (v - 0.55) / 0.34;
    if (x < 0 || x > 1 || y < 0 || y > 1) return false;
    const d1 = Math.abs((y - 0.55) - 0.9 * (x - 0.18)) < 0.17 && x > 0.12 && x < 0.42;
    const d2 = Math.abs((y - 0.72) + 1.15 * (x - 0.40)) < 0.17 && x > 0.36 && x < 0.86;
    return d1 || d2;
  })();
  if (tick) return TICK.concat(255);

  if (inCard) {
    // three form lines
    const lines = [0.32, 0.46, 0.60];
    for (let i = 0; i < lines.length; i++) {
      const w = i === 2 ? 0.26 : 0.38;
      if (Math.abs(v - lines[i]) < 0.045 && u > cx0 + 0.06 && u < cx0 + 0.06 + w) {
        return LINE.concat(255);
      }
    }
    return CARD.concat(255);
  }
  return BG.concat(255);
}

function render(size) {
  const SS = 4;
  const small = size <= 16;
  const px = [];
  for (let y = 0; y < size; y++) {
    for (let x = 0; x < size; x++) {
      let acc = [0, 0, 0, 0];
      for (let sy = 0; sy < SS; sy++) {
        for (let sx = 0; sx < SS; sx++) {
          const u = (x + (sx + 0.5) / SS) / size;
          const v = (y + (sy + 0.5) / SS) / size;
          const s = sample(u, v, small) || [0, 0, 0, 0];
          acc[0] += s[0] * s[3]; acc[1] += s[1] * s[3]; acc[2] += s[2] * s[3]; acc[3] += s[3];
        }
      }
      const n = SS * SS;
      const a = acc[3] / n;
      px.push(a === 0 ? [0, 0, 0, 0]
        : [Math.round(acc[0] / acc[3]), Math.round(acc[1] / acc[3]), Math.round(acc[2] / acc[3]), Math.round(a)]);
    }
  }
  return px;
}

fs.mkdirSync(OUT, { recursive: true });
[16, 32, 48, 128].forEach((size) => {
  const file = `${OUT}icon${size}.png`;
  fs.writeFileSync(file, png(size, render(size)));
  console.log("wrote", file, fs.statSync(file).size, "bytes");
});
