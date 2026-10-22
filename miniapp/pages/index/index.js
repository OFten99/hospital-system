// pages/index/index.js —— 首页：搜索 + 医院头卡 + 双栏科室/医生 + 就医须知
const api = require('../../utils/request.js');

const NOTICE_KEY = 'notice_ack_v1';

const NOTICE_TEXT = '① 预约时间：提前 7 天 15:00 放号，同一就诊人同一天同一医生只可预约 1 次；\n' +
  '② 自费患者支持在线挂号及诊疗缴费；挂号缴费后无需窗口取号，直接出示挂号成功二维码，到护士站签到后按序候诊；\n' +
  '③ 预约后请按预约时段到院候诊，取号截止时间：上午 11:30、下午 16:30；\n' +
  '④ 挂号仅限当日使用，过期作废；若诊疗需要的检查检验项目不能当日完成，请等获取报告后再次挂号就诊；\n' +
  '⑤ 预约成功后，若不能按时就诊，请至少提前一天在「我的挂号」中退号，过时将无法取消，并记 1 次违约，违约累计 3 次，将被列入黑名单（三个月内不享受预约服务）。';

Page({
  data: {
    // 头卡
    hospitalName: '市第一人民医院（门诊部）',
    hospitalLevel: '三级甲等',
    hospitalAddr: '门诊综合楼 1 层挂号收费窗口',
    // 双栏
    departments: [],
    activeDeptId: null,
    activeDeptName: '',
    doctors: [],
    doctorsLoading: false,
    // 搜索
    keyword: '',
    searchResults: null,   // null=未搜索；数组=搜索结果
    // 状态
    loading: true,
    error: '',
    showNotice: false,
    noticeText: NOTICE_TEXT
  },

  onLoad() {
    // 就医须知：未确认过才弹
    if (!wx.getStorageSync(NOTICE_KEY)) {
      this.setData({ showNotice: true });
    }
    this.loadDepartments();
  },

  onPullDownRefresh() {
    this.loadDepartments(() => wx.stopPullDownRefresh());
  },

  onNoticeOk() {
    wx.setStorageSync(NOTICE_KEY, '1');
    this.setData({ showNotice: false });
  },

  onShowNotice() {
    this.setData({ showNotice: true });
  },

  loadDepartments(done) {
    this.setData({ loading: true, error: '' });
    api.getDepartments()
      .then(list => {
        const deps = list || [];
        this.setData({
          departments: deps,
          loading: false,
          // 默认选中第一个科室并加载其医生
          activeDeptId: deps.length ? deps[0].department_id : null,
          activeDeptName: deps.length ? deps[0].department_name : ''
        });
        if (deps.length) this.loadDoctors(deps[0].department_id);
      })
      .catch(() => this.setData({ loading: false, error: '无法连接后端服务，请确认：\n1. 已双击 run_web.bat 启动服务\n2. 详情→本地设置→勾选「不校验合法域名」\n3. app.js 里 baseUrl 正确' }))
      .then(() => done && done());
  },

  loadDoctors(deptId) {
    this.setData({ doctorsLoading: true, doctors: [] });
    api.getDoctors(deptId)
      .then(list => this.setData({ doctors: list || [], doctorsLoading: false }))
      .catch(() => this.setData({ doctorsLoading: false }));
  },

  onTapDepartment(e) {
    const { id, name } = e.currentTarget.dataset;
    this.setData({ activeDeptId: id, activeDeptName: name, keyword: '', searchResults: null });
    this.loadDoctors(id);
  },

  // ---------- 搜索 ----------
  onKeywordInput(e) {
    this.setData({ keyword: e.detail.value });
    if (!e.detail.value.trim()) {
      this.setData({ searchResults: null });  // 清空即恢复双栏
    }
  },

  onSearch() {
    const kw = this.data.keyword.trim();
    if (!kw) {
      this.setData({ searchResults: null });
      return;
    }
    api.search(kw)
      .then(list => this.setData({ searchResults: list || [] }))
      .catch(() => {});
  },

  onClearSearch() {
    this.setData({ keyword: '', searchResults: null });
  },

  // ---------- 跳转 ----------
  goDoctor(e) {
    const { id, name, title } = e.currentTarget.dataset;
    wx.navigateTo({
      url: `/pages/schedules/schedules?doctor_id=${id}&doctor_name=${name}&title=${title}`
    });
  },

  goMine() {
    wx.switchTab({ url: '/pages/mine/mine' });
  }
});
