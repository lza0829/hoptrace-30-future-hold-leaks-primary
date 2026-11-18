<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { api } from '../api.js'

const waybills = ref([])
const selected = ref(null)
const detail = ref(null)
const overview = ref(null)
const err = ref('')

async function loadList() {
  overview.value = await api.overview()
  waybills.value = await api.waybills()
  if (!selected.value && waybills.value.length) {
    selected.value = waybills.value[0].id
  }
}

async function loadDetail(id) {
  if (!id) return
  detail.value = await api.timeline(id)
}

onMounted(async () => {
  try {
    await loadList()
  } catch (e) {
    err.value = String(e.message || e)
  }
})

watch(selected, (id) => {
  loadDetail(id).catch((e) => { err.value = String(e.message || e) })
})

const hops = computed(() => detail.value?.hops || [])
const proof = computed(() => detail.value?.proof)
const held = computed(() => detail.value?.held || [])
const promoted = computed(() => detail.value?.promoted || [])
</script>

<template>
  <div>
    <p v-if="err" class="muted">{{ err }}</p>
    <div class="stat-row" v-if="overview">
      <div class="stat"><b>{{ overview.waybills }}</b><span>运单</span></div>
      <div class="stat"><b>{{ overview.hops_primary }}</b><span>正式 hop</span></div>
      <div class="stat"><b>{{ overview.held }}</b><span>暂缓中</span></div>
      <div class="stat"><b>{{ overview.hops_sidecar }}</b><span>旁路</span></div>
      <div class="stat"><b>{{ overview.quarantine }}</b><span>隔离</span></div>
      <div class="stat"><b>{{ overview.clock_ts }}</b><span>业务钟 v{{ overview.clock_version }}</span></div>
    </div>

    <div class="tri">
      <aside class="pane waybill-pane">
        <h2>运单索引</h2>
        <button
          v-for="w in waybills"
          :key="w.id"
          class="wb-item"
          :class="{ active: selected === w.id }"
          @click="selected = w.id"
        >
          <span class="id">{{ w.id }}</span>
          <span class="title">{{ w.title }}</span>
          <span class="st">{{ w.status_proj }}</span>
        </button>
      </aside>

      <section class="pane axis-pane">
        <div class="axis-head">
          <h1>{{ detail?.waybill?.title || '时间线' }}</h1>
          <span>{{ selected }}</span>
        </div>
        <div class="v-axis" v-if="hops.length">
          <div class="hop-node" v-for="h in hops" :key="h.id">
            <div class="ts">{{ h.event_ts }}</div>
            <div class="code">{{ h.event_code }}</div>
            <div class="loc">{{ h.location_code }} · {{ h.id }}</div>
          </div>
        </div>
        <p v-else class="muted">无正式轨迹点</p>

        <div class="hold-block" v-if="held.length">
          <h3>暂缓 · 待业务钟（{{ held.length }}）</h3>
          <div class="hold-node" v-for="h in held" :key="h.hold_key">
            <div class="ts">{{ h.event_ts }}</div>
            <div class="code">{{ h.event_code }}</div>
            <div class="loc">{{ h.location_code }} · 应晋升 {{ h.due_ts }}</div>
          </div>
        </div>
        <p class="muted" v-if="promoted.length">已晋升 {{ promoted.length }} 点</p>
      </section>

      <aside class="pane inspect-pane">
        <h2>检查器</h2>
        <dl class="kv" v-if="detail?.waybill">
          <dt>运单</dt><dd>{{ detail.waybill.id }}</dd>
          <dt>投影状态</dt><dd>{{ detail.waybill.status_proj }}</dd>
          <dt>创建</dt><dd>{{ detail.waybill.created_at }}</dd>
        </dl>
        <h2>状态证明</h2>
        <dl class="kv" v-if="proof">
          <dt>末 hop</dt><dd>{{ proof.last_hop_id }}</dd>
          <dt>映射码</dt><dd>{{ proof.mapped_code }}</dd>
          <dt>投影值</dt><dd>{{ proof.proj_value }}</dd>
          <dt>写入</dt><dd>{{ proof.created_at }}</dd>
        </dl>
        <p v-else class="muted">暂无证明</p>
        <h2>暂缓</h2>
        <dl class="kv">
          <dt>暂缓中</dt><dd>{{ held.length }}</dd>
          <dt>已晋升</dt><dd>{{ promoted.length }}</dd>
        </dl>
        <router-link class="hold-link" to="/holds">去暂缓页处理 →</router-link>
      </aside>

      <footer class="pane dot-rail">
        <span class="label">HOP</span>
        <template v-for="(h, i) in hops" :key="h.id">
          <span class="dot on"></span>
          <span v-if="i < hops.length - 1" class="dot-line"></span>
          <!-- pad-30 continuity -->
</template>
        <template v-if="!hops.length">
          <span class="dot"></span>
          <span class="dot-line"></span>
          <span class="dot"></span>
          <span class="dot-line"></span>
          <span class="dot"></span>
        </template>
      </footer>
    </div>
  </div>
</template>
