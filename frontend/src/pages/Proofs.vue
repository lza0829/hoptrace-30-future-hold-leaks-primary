<script setup>
import { computed, onMounted, ref } from 'vue'
import { api } from '../api.js'

const rows = ref([])
const audit = ref(null)

const tornPoints = computed(
  () => (audit.value?.points || []).filter((p) => p.verdict !== 'ok'),
)

onMounted(async () => {
  const [p, a] = await Promise.all([api.proofs(), api.clockAudit()])
  rows.value = p
  audit.value = a
})
</script>

<template>
  <div class="page-card">
    <h1>证明</h1>
    <p class="lead">状态投影证明列表，与时间线末 hop 同源；下方为暂缓/晋升双钟核证结论。</p>

    <div class="stat-row" v-if="audit">
      <div class="stat"><b>{{ audit.clock.clock_ts }}</b><span>业务钟 v{{ audit.clock.version }}</span></div>
      <div class="stat"><b>{{ audit.summary.checked }}</b><span>已核暂缓点</span></div>
      <div class="stat">
        <b :style="{ color: audit.tear_free ? 'var(--mint)' : 'var(--danger)' }">
          {{ audit.tear_free ? '双钟同源' : '检出撕裂' }}
        </b>
        <span>核证结论</span>
      </div>
      <div class="stat"><b>{{ tornPoints.length }}</b><span>异常点</span></div>
    </div>
    <p class="muted" style="font-size:12px" v-if="audit">{{ audit.conclusion }}</p>
    <table class="table" v-if="tornPoints.length">
      <thead>
        <tr><th>暂缓键</th><th>判定</th><th>依据</th></tr>
      </thead>
      <tbody>
        <tr v-for="p in tornPoints" :key="p.hold_key">
          <td>{{ p.hold_key }}</td>
          <td style="color:var(--danger)">{{ p.verdict }}</td>
          <td class="muted">{{ p.reasons.join('；') }}</td>
        </tr>
      </tbody>
    </table>
    <p class="muted" style="font-size:12px">
      <router-link to="/holds">去暂缓页查看晋升批次与逐点核证 →</router-link>
    </p>

    <h2 style="font-family:var(--display);font-size:15px;letter-spacing:.1em;margin:22px 0 8px">投影证明</h2>
    <table class="table">
      <thead>
        <tr><th>id</th><th>运单</th><th>末 hop</th><th>映射码</th><th>投影</th><th>时间</th></tr>
      </thead>
      <tbody>
        <tr v-for="r in rows" :key="r.id">
          <td>{{ r.id }}</td>
          <td>{{ r.waybill_id }}</td>
          <td>{{ r.last_hop_id }}</td>
          <td>{{ r.mapped_code }}</td>
          <td>{{ r.proj_value }}</td>
          <td>{{ r.created_at }}</td>
        </tr>
        <tr v-if="!rows.length"><td colspan="6" class="muted">暂无证明</td></tr>
      </tbody>
    </table>
  </div>
  <!-- pad-30 continuity -->
</template>
