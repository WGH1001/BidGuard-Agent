<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus/es/components/message/index'
import { ElMessageBox } from 'element-plus/es/components/message-box/index'
import { authApi } from '@/api'
import type { AdminUserOut, TenantOut, UserRole } from '@/api/types'
import { useAuthStore } from '@/stores/auth'

defineOptions({ name: 'UsersManageView' })

const authStore = useAuthStore()
const router = useRouter()

const loading = ref(false)
const tenants = ref<TenantOut[]>([])
const users = ref<AdminUserOut[]>([])

const newTenantName = ref('')
const creatingTenant = ref(false)

const filterTenantId = ref<number | ''>('')
const searchKeyword = ref('')
const showDisabledUsers = ref(false)

// 新建用户弹窗
const createDialogVisible = ref(false)
const creatingUser = ref(false)
const createForm = reactive({
  username: '',
  display_name: '',
  password: '',
  role: 'member' as UserRole,
  tenant_id: 0,
})

// 编辑用户弹窗
const editDialogVisible = ref(false)
const savingEdit = ref(false)
const editTarget = ref<AdminUserOut | null>(null)
const editForm = reactive({
  username: '',
  display_name: '',
  role: 'member' as UserRole,
  tenant_id: 0,
})

// 重置密码弹窗
const resetPwdDialogVisible = ref(false)
const resettingPwd = ref(false)
const resetPwdTarget = ref<AdminUserOut | null>(null)
const resetPwdValue = ref('')

const activeAdminCount = computed(
  () => users.value.filter((u) => u.role === 'admin' && u.is_active).length,
)

const activeUserCount = computed(
  () => users.value.filter((u) => u.is_active).length,
)

const disabledUserCount = computed(
  () => users.value.length - activeUserCount.value,
)

const filteredUsers = computed(() => {
  const kw = searchKeyword.value.trim().toLowerCase()
  return users.value.filter((u) => {
    if (!showDisabledUsers.value && !u.is_active) {
      return false
    }
    if (filterTenantId.value !== '' && u.tenant_id !== filterTenantId.value) {
      return false
    }
    if (!kw) return true
    return (
      u.username.toLowerCase().includes(kw) ||
      u.display_name.toLowerCase().includes(kw) ||
      u.tenant_name.toLowerCase().includes(kw)
    )
  })
})

function asUser(row: unknown): AdminUserOut {
  return row as AdminUserOut
}

function isSelf(row: AdminUserOut): boolean {
  if (authStore.user?.id != null && row.id === authStore.user.id) return true
  return Boolean(authStore.user?.username && row.username === authStore.user.username)
}

function isLastActiveAdmin(row: AdminUserOut): boolean {
  return row.role === 'admin' && row.is_active && activeAdminCount.value <= 1
}

function formatDateTime(iso?: string | null): string {
  if (!iso) return '—'
  return iso.replace('T', ' ').slice(0, 19)
}

async function loadAll(): Promise<void> {
  loading.value = true
  try {
    const [tenantList, userList] = await Promise.all([
      authApi.listTenants(),
      authApi.listUsers(),
    ])
    tenants.value = tenantList
    users.value = userList
    if (!createForm.tenant_id && tenantList.length > 0) {
      createForm.tenant_id = tenantList[0].id
    }
  } finally {
    loading.value = false
  }
}

async function handleCreateTenant(): Promise<void> {
  const name = newTenantName.value.trim()
  if (!name) {
    ElMessage.warning('请输入部门名称')
    return
  }
  creatingTenant.value = true
  try {
    const created = await authApi.createTenant({ name })
    ElMessage.success(`已新建部门「${created.name}」`)
    newTenantName.value = ''
    await loadAll()
  } catch {
    // 已由全局 http 拦截器提示
  } finally {
    creatingTenant.value = false
  }
}

async function handleRenameTenant(tenant: TenantOut): Promise<void> {
  let promptResult: { value: string }
  try {
    promptResult = await ElMessageBox.prompt(
      `请输入部门「${tenant.name}」的新名称（全系统所有成员与任务展示将同步更新）：`,
      '重命名部门',
      {
        inputValue: tenant.name,
        confirmButtonText: '保存名称',
        cancelButtonText: '取消',
        inputValidator: (val: string) => {
          if (!val || !val.trim()) return '部门名称不能为空'
          if (val.trim().length > 128) return '部门名称长度不能超过 128 个字符'
          return true
        },
      },
    )
  } catch {
    return
  }

  const nextName = promptResult.value.trim()
  if (!nextName || nextName === tenant.name) return
  try {
    const updated = await authApi.updateTenant(tenant.id, { name: nextName })
    ElMessage.success(`部门已重命名为「${updated.name}」`)
    await loadAll()
    if (authStore.user?.tenant_id === tenant.id) {
      await authStore.refreshMe()
    }
  } catch {
    // 已由全局 http 拦截器提示
  }
}

function openCreateUser(): void {
  createForm.username = ''
  createForm.display_name = ''
  createForm.password = ''
  createForm.role = 'member'
  createForm.tenant_id = tenants.value.find((t) => t.is_active)?.id ?? tenants.value[0]?.id ?? 0
  createDialogVisible.value = true
}

async function submitCreateUser(): Promise<void> {
  if (!createForm.username.trim()) {
    ElMessage.warning('请填写登录账号')
    return
  }
  if (createForm.password.trim().length < 6) {
    ElMessage.warning('初始密码不能少于 6 位')
    return
  }
  if (!createForm.tenant_id) {
    ElMessage.warning('请选择所属部门')
    return
  }
  creatingUser.value = true
  try {
    const created = await authApi.createUser({
      username: createForm.username.trim(),
      display_name: createForm.display_name.trim(),
      password: createForm.password,
      role: createForm.role,
      tenant_id: createForm.tenant_id,
    })
    ElMessage.success(`已创建账号「${created.username}」`)
    createDialogVisible.value = false
    await loadAll()
  } catch {
    // 已由全局 http 拦截器提示
  } finally {
    creatingUser.value = false
  }
}

function openEditUser(row: AdminUserOut): void {
  editTarget.value = row
  editForm.username = row.username
  editForm.display_name = row.display_name || row.username
  editForm.role = row.role
  editForm.tenant_id = row.tenant_id
  editDialogVisible.value = true
}

async function submitEditUser(): Promise<void> {
  const target = editTarget.value
  if (!target) return
  const nextUsername = editForm.username.trim()
  if (!nextUsername) {
    ElMessage.warning('请填写登录账号')
    return
  }

  const usernameChanged = nextUsername !== target.username
  const editingSelf = isSelf(target)
  if (usernameChanged) {
    try {
      await ElMessageBox.confirm(
        editingSelf
          ? `您正在修改当前登录管理员的登录账号（${target.username} → ${nextUsername}）。保存后当前登录令牌将立即失效并自动退出至登录页，本地置顶会话需重新置顶，确定继续吗？`
          : `修改登录账号（${target.username} → ${nextUsername}）后，该员工需重新登录，本地置顶会话需重新置顶。确定保存吗？`,
        '修改登录账号确认',
        {
          confirmButtonText: '确认修改',
          cancelButtonText: '取消',
          type: 'warning',
        },
      )
    } catch {
      return
    }
  }

  savingEdit.value = true
  try {
    const updated = await authApi.updateUser(target.id, {
      ...(usernameChanged ? { username: nextUsername } : {}),
      display_name: editForm.display_name.trim() || nextUsername,
      role: editForm.role,
      tenant_id: editForm.tenant_id,
    })
    editDialogVisible.value = false
    if (usernameChanged && editingSelf) {
      ElMessage.success(`登录账号已修改为「${updated.username}」，请重新登录`)
      authStore.logout()
      void router.push('/login')
      return
    }
    ElMessage.success(`已更新账号「${updated.username}」信息`)
    await loadAll()
    if (editingSelf) {
      await authStore.refreshMe()
    }
  } catch {
    // 已由全局 http 拦截器提示
  } finally {
    savingEdit.value = false
  }
}

function openResetPassword(row: AdminUserOut): void {
  resetPwdTarget.value = row
  resetPwdValue.value = ''
  resetPwdDialogVisible.value = true
}

async function submitResetPassword(): Promise<void> {
  const target = resetPwdTarget.value
  if (!target) return
  if (resetPwdValue.value.trim().length < 6) {
    ElMessage.warning('重置密码长度不能少于 6 位')
    return
  }
  resettingPwd.value = true
  try {
    await authApi.resetUserPassword(target.id, {
      new_password: resetPwdValue.value,
    })
    ElMessage.success(`已重置账号「${target.username}」的登录密码`)
    resetPwdDialogVisible.value = false
  } catch {
    // 已由全局 http 拦截器提示
  } finally {
    resettingPwd.value = false
  }
}

async function handleToggleActive(row: AdminUserOut): Promise<void> {
  const nextActive = !row.is_active
  const actionLabel = nextActive ? '启用' : '禁用'
  try {
    await ElMessageBox.confirm(
      nextActive
        ? `确定恢复启用账号「${row.username}（${row.display_name}）」吗？`
        : `禁用后「${row.username}」的现有登录令牌将立即失效且无法再登录，确定继续吗？`,
      `${actionLabel}账号确认`,
      {
        confirmButtonText: `确认${actionLabel}`,
        cancelButtonText: '取消',
        type: nextActive ? 'info' : 'warning',
      },
    )
  } catch {
    return
  }

  try {
    await authApi.updateUser(row.id, { is_active: nextActive })
    ElMessage.success(`已${actionLabel}账号「${row.username}」`)
    await loadAll()
  } catch {
    // 已由全局 http 拦截器提示
  }
}

onMounted(() => {
  void loadAll()
})
</script>

<template>
  <div class="users-manage-page">
    <!-- 顶部部门与租户配置卡片 -->
    <section class="bg-panel section-card">
      <div class="section-hd">
        <div>
          <h2>部门组织台账（Tenant）</h2>
          <p class="section-sub">
            同一部门成员共享规则配置、证照台账与体检任务资产；不同部门严格按租户隔离。
          </p>
        </div>
        <div class="tenant-create-bar">
          <el-input
            v-model.trim="newTenantName"
            placeholder="输入新部门名称，如：华东大区投标中心"
            style="width: 260px"
            clearable
            @keyup.enter="handleCreateTenant"
          />
          <el-button
            type="primary"
            :loading="creatingTenant"
            @click="handleCreateTenant"
          >
            + 新建部门
          </el-button>
        </div>
      </div>

      <div class="tenant-chips">
        <div
          v-for="t in tenants"
          :key="t.id"
          class="tenant-chip"
          :class="{ 'is-disabled': !t.is_active }"
        >
          <span class="t-id mono-num">#{{ t.id }}</span>
          <span class="t-name">{{ t.name }}</span>
          <el-tag size="small" :type="t.is_active ? 'success' : 'info'">
            {{ t.is_active ? '启用中' : '已停用' }}
          </el-tag>
          <el-button link type="primary" size="small" @click="handleRenameTenant(t)">
            重命名
          </el-button>
        </div>
        <div v-if="!loading && tenants.length === 0" class="empty-tip">暂无部门数据</div>
      </div>
    </section>

    <!-- 平台账号管理台账 -->
    <section class="bg-panel section-card">
      <div class="section-hd">
        <div>
          <h2>
            平台成员账号台账（启用 {{ activeUserCount }} / 共 {{ users.length }} 人）
          </h2>
          <p class="section-sub">
            支持为团队成员分配独立账号、修改登录账号与姓名、切换所属部门、重置密码及禁用离职账号<span
              v-if="disabledUserCount > 0 && !showDisabledUsers"
            >（当前已隐藏 {{ disabledUserCount }} 个已禁用账号）</span>。
          </p>
        </div>

        <div class="toolbar-actions">
          <el-switch
            v-model="showDisabledUsers"
            active-text="显示已禁用"
            title="默认隐藏已禁用历史账号；开启后可查看并恢复启用"
          />

          <el-select
            v-model="filterTenantId"
            clearable
            placeholder="全部部门"
            style="width: 190px"
          >
            <el-option label="全部部门" value="" />
            <el-option
              v-for="t in tenants"
              :key="t.id"
              :label="t.name"
              :value="t.id"
            />
          </el-select>

          <el-input
            v-model.trim="searchKeyword"
            placeholder="搜索账号或姓名"
            clearable
            style="width: 200px"
          />

          <el-button @click="loadAll" :loading="loading">刷新</el-button>
          <el-button type="primary" @click="openCreateUser">+ 新建账号</el-button>
        </div>
      </div>

      <el-alert
        type="info"
        :closable="false"
        show-icon
        style="margin-bottom: 14px"
        title="安全护栏：禁用某账号后其既有 JWT 令牌立即失效；当前系统至少保留 1 名活跃管理员，且管理员不可禁用或降级自己。"
      />

      <el-table
        v-loading="loading"
        :data="filteredUsers"
        border
        stripe
        style="width: 100%"
      >
        <el-table-column prop="id" label="ID" width="70" align="center">
          <template #default="{ row }">
            <span class="mono-num">#{{ row.id }}</span>
          </template>
        </el-table-column>

        <el-table-column prop="username" label="登录账号" min-width="140">
          <template #default="{ row }">
            <span class="mono-num user-acct">{{ row.username }}</span>
            <el-tag v-if="isSelf(asUser(row))" size="small" type="warning" style="margin-left: 6px">
              当前登录
            </el-tag>
          </template>
        </el-table-column>

        <el-table-column prop="display_name" label="姓名 / 备注" min-width="140" />

        <el-table-column prop="tenant_name" label="所属部门" min-width="160">
          <template #default="{ row }">
            <span>{{ row.tenant_name || `部门 #${row.tenant_id}` }}</span>
          </template>
        </el-table-column>

        <el-table-column prop="role" label="角色权限" width="130" align="center">
          <template #default="{ row }">
            <el-tag size="small" :type="row.role === 'admin' ? 'danger' : 'info'">
              {{ row.role === 'admin' ? '系统管理员' : '普通成员' }}
            </el-tag>
          </template>
        </el-table-column>

        <el-table-column prop="is_active" label="账号状态" width="110" align="center">
          <template #default="{ row }">
            <el-tag size="small" :type="row.is_active ? 'success' : 'info'">
              {{ row.is_active ? '正常启用' : '已禁用' }}
            </el-tag>
          </template>
        </el-table-column>

        <el-table-column label="最近登录" min-width="165">
          <template #default="{ row }">
            <span class="mono-num muted-time">{{ formatDateTime(row.last_login_at) }}</span>
          </template>
        </el-table-column>

        <el-table-column label="操作" width="230" fixed="right">
          <template #default="{ row }">
            <div class="row-actions">
              <el-button link type="primary" size="small" @click="openEditUser(asUser(row))">
                编辑
              </el-button>
              <el-button link type="primary" size="small" @click="openResetPassword(asUser(row))">
                重置密码
              </el-button>
              <el-button
                link
                size="small"
                :type="row.is_active ? 'danger' : 'success'"
                :disabled="row.is_active && (isSelf(asUser(row)) || isLastActiveAdmin(asUser(row)))"
                :title="
                  isLastActiveAdmin(asUser(row))
                    ? '系统最后 1 名活跃管理员不可禁用'
                    : isSelf(asUser(row))
                      ? '不可禁用当前登录管理员自身'
                      : ''
                "
                @click="handleToggleActive(asUser(row))"
              >
                {{ row.is_active ? '禁用' : '启用' }}
              </el-button>
            </div>
          </template>
        </el-table-column>
      </el-table>
    </section>

    <!-- 新建账号弹窗 -->
    <el-dialog v-model="createDialogVisible" title="新建平台成员账号" width="460px" destroy-on-close>
      <el-form label-width="96px">
        <el-form-item label="登录账号" required>
          <el-input
            v-model.trim="createForm.username"
            placeholder="字母/数字/下划线/点/短横线，如 zhangsan"
          />
        </el-form-item>
        <el-form-item label="姓名/备注">
          <el-input v-model.trim="createForm.display_name" placeholder="如：张三（商务专员）" />
        </el-form-item>
        <el-form-item label="所属部门" required>
          <el-select v-model="createForm.tenant_id" style="width: 100%">
            <el-option
              v-for="t in tenants"
              :key="t.id"
              :label="t.name"
              :value="t.id"
              :disabled="!t.is_active"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="角色权限" required>
          <el-select v-model="createForm.role" style="width: 100%">
            <el-option label="普通成员 (member)" value="member" />
            <el-option label="系统管理员 (admin)" value="admin" />
          </el-select>
        </el-form-item>
        <el-form-item label="初始密码" required>
          <el-input
            v-model="createForm.password"
            type="password"
            show-password
            placeholder="至少 6 位字符"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="createDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="creatingUser" @click="submitCreateUser">
          确认创建
        </el-button>
      </template>
    </el-dialog>

    <!-- 编辑账号弹窗 -->
    <el-dialog v-model="editDialogVisible" title="编辑账号信息与部门角色" width="460px" destroy-on-close>
      <el-form v-if="editTarget" label-width="96px">
        <el-form-item label="登录账号" required>
          <el-input
            v-model.trim="editForm.username"
            placeholder="字母/数字/下划线/点/短横线"
          />
          <div class="field-hint">
            修改登录账号后，该账号现有令牌将立即失效并需重新登录
          </div>
        </el-form-item>
        <el-form-item label="姓名/备注">
          <el-input v-model.trim="editForm.display_name" />
        </el-form-item>
        <el-form-item label="所属部门">
          <el-select v-model="editForm.tenant_id" style="width: 100%">
            <el-option
              v-for="t in tenants"
              :key="t.id"
              :label="t.name"
              :value="t.id"
              :disabled="!t.is_active"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="角色权限">
          <el-select
            v-model="editForm.role"
            style="width: 100%"
            :disabled="isSelf(editTarget) || isLastActiveAdmin(editTarget)"
          >
            <el-option label="普通成员 (member)" value="member" />
            <el-option label="系统管理员 (admin)" value="admin" />
          </el-select>
          <div
            v-if="isSelf(editTarget) || isLastActiveAdmin(editTarget)"
            class="field-hint"
          >
            {{
              isLastActiveAdmin(editTarget)
                ? '系统最后 1 名活跃管理员不可降级'
                : '当前登录管理员不可更改自己的角色'
            }}
          </div>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="editDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="savingEdit" @click="submitEditUser">
          保存修改
        </el-button>
      </template>
    </el-dialog>

    <!-- 重置密码弹窗 -->
    <el-dialog v-model="resetPwdDialogVisible" title="管理员重置登录密码" width="420px" destroy-on-close>
      <el-form v-if="resetPwdTarget" label-width="96px">
        <el-form-item label="目标账号">
          <span class="mono-num">{{ resetPwdTarget.username }}（{{ resetPwdTarget.display_name }}）</span>
        </el-form-item>
        <el-form-item label="新密码" required>
          <el-input
            v-model="resetPwdValue"
            type="password"
            show-password
            placeholder="请输入至少 6 位新密码"
            @keyup.enter="submitResetPassword"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="resetPwdDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="resettingPwd" @click="submitResetPassword">
          确认重置
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.users-manage-page {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.section-card {
  padding: 18px 20px;
}

.section-hd {
  display: flex;
  flex-wrap: wrap;
  justify-content: space-between;
  align-items: flex-start;
  gap: 12px;
  margin-bottom: 14px;
}

.section-hd h2 {
  margin: 0 0 4px 0;
  font-size: 16px;
  font-weight: 700;
  color: #0f172a;
}

.section-sub {
  margin: 0;
  font-size: 12.5px;
  color: #64748b;
}

.tenant-create-bar,
.toolbar-actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}

.tenant-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
}

.tenant-chip {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  padding: 6px 12px;
  background: #f8fafc;
  border: 1px solid #e2e8f0;
  border-radius: 6px;
  font-size: 13px;
}

.tenant-chip.is-disabled {
  opacity: 0.65;
}

.t-id {
  font-size: 11.5px;
  color: #64748b;
}

.t-name {
  font-weight: 600;
  color: #1e293b;
}

.user-acct {
  font-weight: 600;
  color: #0f172a;
}

.muted-time {
  font-size: 12px;
  color: #64748b;
}

.row-actions {
  display: flex;
  align-items: center;
  gap: 6px;
}

.field-hint {
  margin-top: 4px;
  font-size: 12px;
  color: #b45309;
  line-height: 1.3;
}

.empty-tip {
  font-size: 13px;
  color: #94a3b8;
}
</style>
