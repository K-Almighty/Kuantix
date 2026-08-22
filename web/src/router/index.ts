import { createRouter, createWebHistory } from 'vue-router';
import Backtest from '../views/Backtest.vue';
import Compare from '../views/CompareView.vue';
import Factors from '../views/Factors.vue';
import Monitor from '../views/Monitor.vue';
import Optimize from '../views/OptimizeView.vue';
import Portfolio from '../views/PortfolioView.vue';
import Screen from '../views/Screen.vue';
import Strategies from '../views/StrategiesView.vue';
import PreOpen from '../views/PreOpen.vue';
import PostClose from '../views/PostClose.vue';
import StockDetail from '../views/StockDetail.vue';

const router = createRouter({
  history: createWebHistory(import.meta.env.BASE_URL),
  routes: [
    { path: '/', redirect: '/factors' },
    { path: '/factors', name: 'factors', component: Factors },
    { path: '/pre-open', name: 'pre-open', component: PreOpen },
    { path: '/post-close', name: 'post-close', component: PostClose },
    { path: '/monitor', name: 'monitor', component: Monitor },
    { path: '/screen', name: 'screen', component: Screen },
    { path: '/backtest', name: 'backtest', component: Backtest },
    { path: '/portfolio', name: 'portfolio', component: Portfolio },
    { path: '/optimize', name: 'optimize', component: Optimize },
    { path: '/compare', name: 'compare', component: Compare },
    { path: '/strategies', name: 'strategies', component: Strategies },
    { path: '/stock/:code', name: 'stock-detail', component: StockDetail },
    { path: '/:pathMatch(.*)*', redirect: '/factors' },
  ],
});

export default router;
