<script setup lang="ts">
import { computed } from "vue";
import type { Task } from "../api/types";
import { formatTime, relativeTime, weekText } from "../utils";

const props = defineProps<{ task: Task; index: number; dirty: boolean; expanded: boolean }>();
const emit = defineEmits<{
  (e: "toggle"): void;
  (e: "run", id: number): void;
  (e: "position", where: "top" | "bottom"): void;
}>();

function onPosition(where: "top" | "bottom") {
  emit("position", where);
}

const WEEK_CN = ["日", "一", "二", "三", "四", "五", "六"];

// cron 五段的取值范围，与后端 CronTrigger.from_crontab 同口径。周字段刻意是 0-6：
// APScheduler 的 crontab 不认 7（`cron:0 9 * * 7` 后端直接拒），前端若按「0 与 7 都是周日」渲染
// 就会宣传一个根本没建起来的专属作业，实际悄悄回落到全局 sweep。
const CRON_RANGE: [number, number][] = [
  [0, 59],
  [0, 23],
  [1, 31],
  [1, 12],
  [0, 6],
];
// 单段文法：* / 数字 / 名字（月、周段才有 MON、JAN 这类），可带 -区间 与 /步长，逗号成列表
const CRON_ITEM = /^(?:\*|\d+|[A-Za-z]{3,9})(?:-(?:\d+|[A-Za-z]{3,9}))?(?:\/(?:\*|\d+))?$/;

function cronFieldOk(field: string, index: number): boolean {
  const [lo, hi] = CRON_RANGE[index];
  const allowsName = index >= 3; // 只有月段(3)与周段(4)有英文名
  return field.split(",").every((item) => {
    if (!CRON_ITEM.test(item)) return false;
    if (!allowsName && /[A-Za-z]/.test(item)) return false;
    const nums = item.match(/\d+/g)?.map(Number) ?? [];
    if (nums.some((n) => n < lo || n > hi)) return false;
    const range = /^(\d+)-(\d+)/.exec(item);
    return !range || Number(range[1]) <= Number(range[2]); // 区间不许倒挂（2-1 后端同样拒）
  });
}

/** 后端 has_valid_schedule 会不会收下这个 schedule —— 收下才给它建专属作业，才「不归全局 sweep 管」。
 *  刻意不引 cron 解析库，只镜像判定口径里肉眼可见的部分：前缀、段数、数字越界、区间倒挂。 */
function hasOwnSchedule(raw: string): boolean {
  if (!raw) return false;
  if (/^interval:/i.test(raw)) return /^interval: *[+-]?\d+$/i.test(raw); // 后端 int() 失败即拒，成功则 max(1,N) 钳位
  const body = /^cron:(.+)$/i.exec(raw)?.[1]?.trim();
  if (body === undefined) return false; // 没有 interval:/cron: 前缀，后端一概不收
  const fields = body.split(/\s+/);
  return fields.length === 5 && fields.every(cronFieldOk); // from_crontab 只吃 5 段
}

/** 更新频率口语化：本项目只有 interval:分钟 与 cron:<标准 5 段> 两种写法，够用了。
 *  能达意的预设（每 N 分钟 / 每天 HH:MM / 每周X HH:MM）说人话，其余（自定义 cron、interval:0 这种
 *  被后端钳到 1 分钟的值）返回空串交给调用方原样显示 —— 空串只代表「我说不清」，不代表「继承全局」。 */
function humanFrequency(raw: string): string {
  const interval = /^interval:( *[+-]?\d+)$/i.exec(raw);
  if (interval) {
    const mins = Number(interval[1]);
    return mins > 0 ? `每 ${mins} 分钟` : ""; // <=0 后端钳成 1 分钟，与其编个「每 0 分钟」不如原样显示
  }
  const body = /^cron:(.+)$/i.exec(raw)?.[1]?.trim();
  const fields = (body || "").split(/\s+/);
  if (fields.length !== 5) return "";
  const [minute, hour, dom, month, dow] = fields;
  // 只口语化「分/时 + 每月每天」这一族；带日期区间或月份限定的交给原始值，别硬编
  if (dom !== "*" || month !== "*" || !/^\d{1,2}$/.test(minute) || !/^\d{1,2}$/.test(hour)) return "";
  if (Number(minute) > 59 || Number(hour) > 23) return "";
  const time = `${hour.padStart(2, "0")}:${minute.padStart(2, "0")}`;
  if (dow === "*") return `每天 ${time}`;
  if (!/^\d(?:,\d){0,6}$/.test(dow)) return "";
  const nums = dow.split(",").map((d) => Number(d));
  if (nums.some((d) => d > 6)) return ""; // 7 后端不认（见 CRON_RANGE），这里也跟着当认不出
  const days = [...new Set(nums)];
  return `每周${days.map((d) => WEEK_CN[d]).join("、")} ${time}`;
}

/** 频率徽标文案。「继承全局」只有在后端真的不给它建作业（schedule 空／非法值回退）时才是实话；
 *  形如 cron: 每 5 分钟（星号斜杠开头）、interval:0 这种后端照收的自定义值必须原样显示，否则就是骗用户。 */
function frequencyChip(schedule: string): string {
  const raw = (schedule || "").trim();
  const nice = humanFrequency(raw);
  if (nice) return `频率 ${nice}`;
  return hasOwnSchedule(raw) ? `频率 ${raw}` : "继承全局";
}

// 徽标取值互斥、按此顺序第一个命中即用（spec 4.6 原清单，与后端 once_next_driver 同序）：
// 已过截止 → 已完成 → 重试已用尽 → 重试中 N/3 → 一次性待执行。
// 预算判据必须排在 next_retry_at 之前：矛盾态行（用尽 + 库里还挂着到点时间，只有手工改库或老库残留
// 才会产生）若先看到点时间，就会显「重试中 3/3」并给出一个后端永远不会驱动的 ETA —— 后端刻意先查预算
// 正是为了不给本该停摆的行复活一条命，界面跟着反着判就是谎报。
const ONCE_RETRY_LIMIT_TEXT = 3; // 手工抄自 backend/services/task_service.py 的 ONCE_RETRY_LIMIT，不是自动派生；后端改值必须同步改这里

/** 与后端 enddate_passed（`date.today() > enddate`）同一口径：enddate 当天整天有效，次日起才算过期。
 *  只做纯日期比较，不看本地时分秒，免得界面比后端早一天/一秒判过期；空串或非法日期返回 false，
 *  与后端 strptime 解析失败时"不挡路"一致。 */
function enddatePassed(enddate: string): boolean {
  const m = /^(\d{4})-(\d{1,2})-(\d{1,2})$/.exec(enddate || "");
  if (!m) return false;
  const month = Number(m[2]);
  const day = Number(m[3]);
  if (month < 1 || month > 12 || day < 1 || day > 31) return false; // 后端 strptime 同样拒这种值，这里当作未过期
  const now = new Date();
  const today = now.getFullYear() * 10000 + (now.getMonth() + 1) * 100 + now.getDate();
  return today > Number(m[1]) * 10000 + month * 100 + day;
}

const chips = computed(() => {
  const t = props.task;
  const list: { key: string; text: string; primary?: boolean; success?: boolean; title?: string }[] = [];
  // 执行方式徽标排在形态细节之前；「已完成」由 once + 已停用派生（无法区分停用来源，已知瑕疵）
  if (t.run_mode === "manual") list.push({ key: "m", text: "仅手动" });
  if (t.run_mode === "once") {
    if (enddatePassed(t.enddate)) list.push({ key: "o", text: "已过截止" });
    else if (t.disabled) list.push({ key: "o", text: "已完成", success: true });
    else if (t.retry_attempts >= ONCE_RETRY_LIMIT_TEXT)
      list.push({ key: "o", text: "重试已用尽", title: "点 ▶ 运行重新开启" });
    else if (t.next_retry_at)
      list.push({
        key: "o",
        text: `重试中 ${t.retry_attempts}/${ONCE_RETRY_LIMIT_TEXT}`,
        title: `下次重试：${formatTime(t.next_retry_at)}`,
      });
    else list.push({ key: "o", text: "一次性待执行", primary: true });
  }
  if (t.run_mode === "follow" && t.schedule) {
    // 自定义但合法的频率后端会给它建专属作业（task_service 也拒绝让全局 sweep 驱动它），
    // 这时候说「继承全局」就是谎报；口语化不出来的原样显示。
    list.push({ key: "s", text: frequencyChip(t.schedule) });
  }
  if (t.pattern) list.push({ key: "p", text: `正则 ${t.pattern}`, primary: true });
  if (t.replace) list.push({ key: "r", text: `替换 ${t.replace}` });
  if (t.ignore_extension) list.push({ key: "e", text: "忽略扩展名" });
  if (t.update_subdir) {
    list.push({ key: "u", text: `子目录追更${t.update_subdir_resave ? "[重存]" : "[递归]"}` });
  }
  if (t.startfid) list.push({ key: "sf", text: "已选起始文件" });
  if (t.auto_download) list.push({ key: "dl", text: `本地下载${t.download_subdir ? "[含子目录]" : ""}` });
  if (t.enddate) list.push({ key: "d", text: `截止 ${t.enddate}` });
  if (t.runweek && t.runweek.length && t.runweek.length < 7) list.push({ key: "w", text: weekText(t.runweek) });
  return list;
});

const lastRun = computed(() => relativeTime(props.task.last_run_at));

/** 一次性任务停用即「已完成」：此时不再显示灰「停用」，改由绿徽标说明状态。 */
const isOnceDone = computed(() => props.task.run_mode === "once" && props.task.disabled);
</script>

<template>
  <div class="row card" :class="{ expanded }" @click="emit('toggle')">
    <div class="idx">
      {{ index + 1 }}
    </div>
    <div class="main">
      <div class="title-line">
        <span class="name">{{ task.taskname }}</span>
        <span v-if="dirty" class="dot-unsaved" title="未保存" />
        <span v-if="task.shareurl_ban" class="badge badge--danger">失效</span>
        <span v-if="task.disabled && !isOnceDone" class="badge badge--muted">停用</span>
        <span v-else-if="lastRun" class="badge badge--muted">{{ lastRun }}</span>
      </div>
      <div class="path text-muted mono">→ {{ task.savepath }}</div>
      <div class="url text-muted mono">
        {{ task.shareurl }}
      </div>
      <div class="chips">
        <span
          v-for="c in chips"
          :key="c.key"
          class="chip"
          :class="{ 'is-primary': c.primary, 'is-success': c.success }"
          :title="c.title"
          >{{ c.text }}</span
        >
      </div>
    </div>
    <div class="ops" @click.stop>
      <el-button size="small" text @click="emit('run', task.id)"> ▶ 运行 </el-button>
      <el-dropdown trigger="click" @command="onPosition">
        <el-button size="small" text title="排序"> ⋮ </el-button>
        <template #dropdown>
          <el-dropdown-menu>
            <el-dropdown-item command="top">置顶</el-dropdown-item>
            <el-dropdown-item command="bottom">置底</el-dropdown-item>
          </el-dropdown-menu>
        </template>
      </el-dropdown>
      <span class="caret">{{ expanded ? "▲" : "▼" }}</span>
    </div>
  </div>
</template>

<style scoped>
.row {
  display: flex;
  gap: 12px;
  align-items: flex-start;
  cursor: pointer;
  transition: box-shadow 0.15s;
}
.row:hover {
  box-shadow: 0 4px 14px rgba(0, 0, 0, 0.05);
}
.row.expanded {
  border-color: var(--primary);
}
.idx {
  width: 22px;
  color: var(--text-muted);
  font-size: 13px;
  text-align: right;
  padding-top: 2px;
}
.main {
  flex: 1;
  min-width: 0;
}
.title-line {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.name {
  font-weight: 600;
  font-size: 15px;
}
.path {
  font-size: 12.5px;
  margin-top: 3px;
}
.url {
  font-size: 12px;
  margin-top: 2px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.chips {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 8px;
}
.ops {
  display: flex;
  align-items: center;
  gap: 4px;
}
.caret {
  color: var(--text-muted);
  font-size: 12px;
}
</style>
