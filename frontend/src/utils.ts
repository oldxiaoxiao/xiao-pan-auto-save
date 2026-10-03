/** 通用格式化工具。 */

/** 相对时间：中文（几分钟前 / 几小时前 / 几天前）。 */
export function relativeTime(iso: string | null | undefined): string {
  if (!iso) return "";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "";
  const diff = Date.now() - then;
  if (diff < 0) return "刚刚";
  const min = Math.floor(diff / 60000);
  if (min < 1) return "刚刚";
  if (min < 60) return `${min} 分钟前`;
  const hr = Math.floor(min / 60);
  if (hr < 24) return `${hr} 小时前`;
  const day = Math.floor(hr / 24);
  if (day < 30) return `${day} 天前`;
  return new Date(iso).toLocaleDateString("zh-CN");
}

/** 字节 → 人类可读。size 后端按字节给（0 时显示空）。 */
export function formatSize(bytes: number | undefined | null): string {
  if (!bytes || bytes <= 0) return "";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let v = bytes;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i += 1;
  }
  return `${v >= 100 || i === 0 ? Math.round(v) : v.toFixed(1)} ${units[i]}`;
}

/** GB 容量显示（账号 capacity 后端按字节）。 */
export function formatGB(bytes: number): string {
  if (!bytes) return "0";
  return (bytes / 1024 ** 3).toFixed(bytes / 1024 ** 3 >= 10 ? 0 : 1);
}

/** 星期数字 → 中文短名（周一=1）。 */
export const WEEK_LABELS = ["一", "二", "三", "四", "五", "六", "日"];

export function weekText(runweek: number[]): string {
  if (!runweek || runweek.length === 0 || runweek.length === 7) return "每天";
  return [...runweek]
    .sort()
    .map((d) => `周${WEEK_LABELS[d - 1] ?? d}`)
    .join(" ");
}

/** mtime 可能是秒/毫秒时间戳或字符串，尽量格式化。 */
export function formatTime(mtime: string | number): string {
  if (mtime === "" || mtime == null) return "";
  let ts = typeof mtime === "number" ? mtime : Number(mtime);
  if (!Number.isNaN(ts) && String(mtime).match(/^\d+$/)) {
    if (ts < 1e12) ts *= 1000;
    return new Date(ts).toLocaleString("zh-CN", { hour12: false });
  }
  const d = new Date(mtime);
  if (!Number.isNaN(d.getTime())) return d.toLocaleString("zh-CN", { hour12: false });
  return String(mtime);
}
