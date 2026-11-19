import { createApp } from 'vue'
import { createPinia } from 'pinia'
import { ElLoadingDirective } from 'element-plus/es/components/loading/index'

import App from './App.vue'
import router from './router'
import './styles/main.css'

import 'element-plus/es/components/message/style/css'
import 'element-plus/es/components/message-box/style/css'
import 'element-plus/es/components/notification/style/css'
import 'element-plus/es/components/loading/style/css'

const app = createApp(App)

app.use(createPinia())
app.use(router)
app.directive('loading', ElLoadingDirective)

app.mount('#app')