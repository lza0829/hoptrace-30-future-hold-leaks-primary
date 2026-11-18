<script setup>
import { computed, onMounted, ref } from 'vue'
import { api } from '../api.js'

const clockInput = ref('')
const clock = ref(null)
const holds = ref([])
const proofs = ref([])
const busy = ref(false)
const msg = ref('')
const err = ref('')

const heldRows = computed(() => holds.value.filter((h) => h.status === 'held'))
const promotedRows = computed(() => holds.value.filter((h) => h.status === 'promoted'))

async function load() {
  const [c, h, p] = await Promise.all([api.clock(), api.holds(), api.holdProofs()])
  clock.value = c
  holds.value = h
  proofs.value = p
  if (!clockInput.value) clockInput.value = c.clock_ts
}

async function run(fn) {
  busy.value = true
  msg.value = ''
  err.value = ''
  try {
    const out = await fn()
    msg.value = typeof out === 'string' ? out : JSON.stringify(out, null, 2)
    await load()
  } catch (e) {
    err.value = String(e.message || e)
  } finally {
    busy.value = false
  }
}

const applyClock = () =>
  run(async () => {
    await api.setClock(clockInput.value)
    return `业务钟已设为 ${clockInput.value}（仅影响之后的分流与晋升）`
  })
const promoteDue = () => run(() => api.promote())

onMounted(() => {
  load().catch((e) => {
    err.value = String(e.message || e)
  })
})
</script>

<template>
  <div class="page-card">
    <h1>暂缓</h1>
    <p class="lead">
      event_ts 晚于业务钟的轨迹点先落暂缓表，不进正式时间线；业务钟走到后显式晋升或到达扫描才入正式线并改投影。
    </p>

    <div class="stat-row" v-if="clock">
      <div class="stat"><b>{{ clock.clock_ts }}</b><span>业务钟</span></div>
      <div class="stat"><b>v{{ clock.version }}</b><span>钟版本</span></div>
      <div class="stat"><b>{{ heldRows.length }}</b><span>暂缓中</span></div>
      <div class="stat"><b>{{ promotedRows.length }}</b><span>已晋升</span></div>
    </div>

    <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:14px">
      <input
        v-model="clockInput"
        placeholder="2026-11-17T20:00:00Z"
        style="flex:1;min-width:240px;padding:9px 10px;border:1px solid var(--line);font-family:var(--mono)"
      />
      <button class="btn" :disabled="busy" @click="applyClock">设定业务钟</button>
      <button class="btn" :disabled="busy" @click="promoteDue">到达扫描 · 晋升到期点</button>
    </div>
    <p v-if="msg" class="muted" style="white-space:pre-wrap;font-family:var(--mono);font-size:12px">{{ msg }}</p>
    <p v-if="err" style="color:var(--danger);font-size:13px">{{ err }}</p>

    <h2 style="font-family:var(--display);font-size:15px;letter-spacing:.1em;margin:22px 0 8px">暂缓集合（held）</h2>
    <table class="table">
      <thead>
        <tr><th>暂缓键</th><th>运单</th><th>event_ts</th><th>地点</th><th>事件</th><th>应晋升时刻</th><th>钟版本</th></tr>
      </thead>
      <tbody>
        <tr v-for="r in heldRows" :key="r.hold_key">
          <td>{{ r.hold_key }}</td>
          <td>{{ r.waybill_id }}</td>
          <td>{{ r.event_ts }}</td>
          <td>{{ r.location_code }}</td>
          <td>{{ r.event_code }}</td>
          <td>{{ r.due_ts }}</td>
          <td>v{{ r.clock_version }}</td>
        </tr>
        <tr v-if="!heldRows.length"><td colspan="7" class="muted">暂无暂缓点</td></tr>
      </tbody>
    </table>

    <h2 style="font-family:var(--display);font-size:15px;letter-spacing:.1em;margin:22px 0 8px">已晋升（promoted）</h2>
    <table class="table">
      <thead>
        <tr><th>暂缓键</th><th>运单</th><th>event_ts</th><th>事件</th><th>晋升时刻</th><th>晋升时钟版本</th></tr>
      </thead>
      <tbody>
        <tr v-for="r in promotedRows" :key="r.hold_key">
          <td>{{ r.hold_key }}</td>
          <td>{{ r.waybill_id }}</td>
          <td>{{ r.event_ts }}</td>
          <td>{{ r.event_code }}</td>
          <td>{{ r.promoted_at }}</td>
          <td>v{{ r.promoted_clock_version }}</td>
        </tr>
        <tr v-if="!promotedRows.length"><td colspan="6" class="muted">暂无晋升记录</td></tr>
      </tbody>
    </table>

    <h2 style="font-family:var(--display);font-size:15px;letter-spacing:.1em;margin:22px 0 8px">暂缓证明</h2>
    <table class="table">
      <thead>
        <tr><th>id</th><th>范围</th><th>键</th><th>业务钟</th><th>钟版本</th><th>暂缓</th><th>晋升</th><th>结论</th></tr>
      </thead>
      <tbody>
        <tr v-for="p in proofs" :key="p.id">
          <td>{{ p.id }}</td>
          <td>{{ p.scope }}</td>
          <td>{{ p.scope_key }}</td>
          <td>{{ p.clock_ts }}</td>
          <td>v{{ p.clock_version }}</td>
          <td>{{ p.held_count }}<span class="muted" v-if="p.held_count"> · {{ p.held_keys.join(' / ') }}</span></td>
          <td>{{ p.promoted_count }}<span class="muted" v-if="p.promoted_count"> · {{ p.promoted_keys.join(' / ') }}</span></td>
          <td>{{ p.conclusion }}</td>
        </tr>
        <tr v-if="!proofs.length"><td colspan="8" class="muted">暂无暂缓证明</td></tr>
      </tbody>
    </table>
  </div>
</template>
