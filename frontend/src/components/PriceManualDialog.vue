<script setup lang="ts">
import { reactive, ref, watch } from 'vue'
import { ElMessage } from 'element-plus/es/components/message/index'
import { checksApi } from '@/api'
import {
  formatMoneyDisplay,
  type BidFindingOut,
  type PriceItemInput,
  type PriceManualBackfillOut,
} from '@/api/types'

const props = defineProps<{
  visible: boolean
  taskId: number
  initialPriceCap?: string | null
  initialBidTotal?: string | null
  initialReferenceBidTotal?: string | null
  initialTableTotal?: string | null
  initialBidTotalUpper?: string | null
  initialItems?: PriceItemInput[] | null
}>()

const emit = defineEmits<{
  (e: 'update:visible', val: boolean): void
  (e: 'completed', res: PriceManualBackfillOut): void
}>()

const submitting = ref(false)
const returnedFindings = ref<BidFindingOut[]>([])

function createEmptyItemRow(): PriceItemInput {
  return { name: '', qty: '1', unit_price: '', total_price: '' }
}

function cleanNumStr(raw?: string | null): string {
  return String(raw || '').replace(/,/g, '').trim()
}

const form = reactive({
  items: [createEmptyItemRow()] as PriceItemInput[],
  table_total: '',
  bid_total: '',
  bid_total_upper: '',
  price_cap: '',
})

function syncFormFromProps(): void {
  returnedFindings.value = []
  const incomingItems = Array.isArray(props.initialItems)
    ? props.initialItems
        .filter((it) => it && (it.name || it.unit_price || it.total_price))
        .map((it) => ({
          name: String(it.name || '').trim(),
          qty: cleanNumStr(it.qty) || '1',
          unit_price: cleanNumStr(it.unit_price),
          total_price: cleanNumStr(it.total_price),
        }))
    : []
  form.items = incomingItems.length > 0 ? incomingItems : [createEmptyItemRow()]
  form.table_total = cleanNumStr(props.initialTableTotal)
  form.bid_total = cleanNumStr(props.initialBidTotal) || cleanNumStr(props.initialReferenceBidTotal)
  form.bid_total_upper = String(props.initialBidTotalUpper || '').trim()
  form.price_cap = cleanNumStr(props.initialPriceCap)
}

watch(
  () => [props.visible, props.taskId] as const,
  ([vis]) => {
    if (vis) {
      syncFormFromProps()
    }
  },
)

function addRow(): void {
  form.items.push(createEmptyItemRow())
}

function removeRow(index: number): void {
  if (form.items.length <= 1) return
  form.items.splice(index, 1)
}

async function handleSubmit(): Promise<void> {
  const validItems = form.items
    .map((i) => ({
      name: i.name.trim(),
      qty: cleanNumStr(i.qty),
      unit_price: cleanNumStr(i.unit_price),
      total_price: cleanNumStr(i.total_price),
    }))
    .filter((i) => i.name && i.qty && i.unit_price && i.total_price)

  if (validItems.length === 0 && !cleanNumStr(form.bid_total)) {
    ElMessage.warning('请至少填写一条有效分项报价或开标一览表总报价')
    return
  }

  submitting.value = true
  try {
    const res = await checksApi.priceManual(props.taskId, {
      items: validItems,
      table_total: cleanNumStr(form.table_total) || null,
      bid_total: cleanNumStr(form.bid_total) || null,
      bid_total_upper: form.bid_total_upper.trim() || null,
      price_cap: cleanNumStr(form.price_cap) || null,
    })
    returnedFindings.value = res.findings || []
    emit('completed', res)

    if (returnedFindings.value.length === 0) {
      ElMessage.success('报价核验通过（分项乘加、汇总、大小写与最高限价全部一致）')
      emit('update:visible', false)
    } else {
      ElMessage.error(`报价沙盒检出 ${returnedFindings.value.length} 处算术或大写不一致硬伤，请查看下方红色告警`)
    }
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <el-dialog
    :model-value="props.visible"
    title="报价表人工回填与精确复算"
    width="860px"
    append-to-body
    :close-on-click-modal="false"
    @update:model-value="(v: boolean) => emit('update:visible', v)"
  >
    <div class="pm-body">
      <div class="pm-notice">
        系统检测到当前投标书报价表为异形合并单元格或未定位到标准金额列。
        请核对或录入以下分项与总价数据，后端将执行高精度 Decimal 复算（差 1 分钱或大写错 1 字立即报警）。
      </div>

      <div class="pm-sec-hd">
        <span>1. 分项报价明细表（金额单位：元，自动展示千分位预览）</span>
        <el-button size="small" type="primary" plain @click="addRow">+ 新增分项行</el-button>
      </div>

      <el-table :data="form.items" border size="small" style="width: 100%; margin-bottom: 16px">
        <el-table-column label="品目/分项名称" min-width="200">
          <template #default="{ row }">
            <el-input v-model="row.name" size="small" placeholder="例如：态势感知软件" />
          </template>
        </el-table-column>

        <el-table-column label="数量" width="105">
          <template #default="{ row }">
            <el-input v-model="row.qty" size="small" placeholder="如 2" />
          </template>
        </el-table-column>

        <el-table-column label="投标单价 (元)" width="175">
          <template #default="{ row }">
            <el-input v-model="row.unit_price" size="small" placeholder="如 42500.00" />
            <div class="fmt-hint mono-num">¥{{ formatMoneyDisplay(row.unit_price) }}</div>
          </template>
        </el-table-column>

        <el-table-column label="投标合价 (元)" width="175">
          <template #default="{ row }">
            <el-input v-model="row.total_price" size="small" placeholder="如 85000.00" />
            <div class="fmt-hint mono-num">¥{{ formatMoneyDisplay(row.total_price) }}</div>
          </template>
        </el-table-column>

        <el-table-column label="操作" width="70" align="center">
          <template #default="{ $index }">
            <el-button
              link
              type="danger"
              :disabled="form.items.length <= 1"
              @click="removeRow($index)"
            >
              删除
            </el-button>
          </template>
        </el-table-column>
      </el-table>

      <div class="pm-sec-hd">
        <span>2. 汇总行、开标一览表总报价与招标最高限价（单位：元）</span>
      </div>

      <div class="pm-grid-2">
        <div class="pm-field">
          <label>分项报价表「合计」行金额 (元)</label>
          <el-input v-model="form.table_total" placeholder="例如：485500.00" />
          <span class="fmt-hint mono-num">展示：¥{{ formatMoneyDisplay(form.table_total) }}</span>
        </div>

        <div class="pm-field">
          <label>开标一览表「投标总报价」小写 (元)</label>
          <el-input v-model="form.bid_total" placeholder="例如：485500.00" />
          <span class="fmt-hint mono-num">展示：¥{{ formatMoneyDisplay(form.bid_total) }}</span>
        </div>

        <div class="pm-field">
          <label>开标一览表「投标总报价」人民币大写</label>
          <el-input v-model="form.bid_total_upper" placeholder="例如：肆拾捌万伍仟伍佰元整" />
        </div>

        <div class="pm-field">
          <label>招标文件规定「最高限价 / 预算上限」 (元)</label>
          <el-input v-model="form.price_cap" placeholder="例如：1450000.00（无则留空）" />
          <span class="fmt-hint mono-num">展示：¥{{ formatMoneyDisplay(form.price_cap) }}</span>
        </div>
      </div>

      <!-- 当返回 findings 非空时，在弹窗内醒目展示红色算术错误 -->
      <div v-if="returnedFindings.length > 0" class="pm-error-box">
        <div class="pe-title">
          检出 {{ returnedFindings.length }} 处一票否决级报价算术/大写硬伤（已同步写入任务风险清单）：
        </div>
        <div v-for="f in returnedFindings" :key="f.id" class="pe-item">
          <span class="sev-pill error">{{ f.code }}</span>
          <span class="pe-msg">{{ f.message }}</span>
        </div>
      </div>
    </div>

    <template #footer>
      <el-button @click="emit('update:visible', false)">关闭</el-button>
      <el-button type="primary" :loading="submitting" @click="handleSubmit">
        提交并执行 F2 算术沙盒核验
      </el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.pm-body {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.pm-notice {
  padding: 10px 14px;
  background: #fffbeb;
  border: 1px solid #fde68a;
  border-left: 4px solid #d97706;
  border-radius: 6px;
  font-size: 12.5px;
  color: #92400e;
  line-height: 1.55;
}

.pm-sec-hd {
  display: flex;
  justify-content: space-between;
  align-items: center;
  font-size: 13px;
  font-weight: 800;
  color: var(--txt-strong);
}

.fmt-hint {
  font-size: 11px;
  color: var(--txt-muted);
  margin-top: 2px;
}

.pm-grid-2 {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 14px;
}

.pm-field label {
  display: block;
  font-size: 12px;
  font-weight: 700;
  color: var(--txt-sec);
  margin-bottom: 5px;
}

.pm-error-box {
  margin-top: 8px;
  padding: 12px 14px;
  background: var(--sev-error-bg);
  border: 1px solid var(--sev-error-border);
  border-left: 4px solid var(--sev-error);
  border-radius: 8px;
}

.pe-title {
  font-size: 13px;
  font-weight: 900;
  color: var(--sev-error);
  margin-bottom: 8px;
}

.pe-item {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 6px;
  font-size: 12.5px;
  color: #7f1d1d;
}
</style>