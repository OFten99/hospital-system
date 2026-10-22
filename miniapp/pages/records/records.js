// pages/records/records.js —— 我的预约（就诊人 + 时间范围筛选）
const api = require('../../utils/request.js');

const RANGES = [
  { label: '近一周', days: 7 },
  { label: '近一个月', days: 30 },
  { label: '近三个月', days: 90 },
  { label: '全部', days: 0 }
];

Page({
  data: {
    idCard: '',
    patientId: null,
    patientName: '',
    registrations: [],
    filtered: [],
    loading: false,
    rangeLabel: '近一个月'
  },

  onLoad() {
    const cached = wx.getStorageSync('id_card');
    const cachedName = wx.getStorageSync('patient_name');
    if (cached) {
      this.setData({ idCard: cached, patientName: cachedName || '' });
      this.search();
    }
  },

  onInput(e) {
    this.setData({ idCard: e.detail.value });
  },

  search() {
    const idCard = this.data.idCard.trim();
    if (!idCard) {
      wx.showToast({ title: '请输入身份证号', icon: 'none' });
      return;
    }
    wx.setStorageSync('id_card', idCard);
    this.setData({ loading: true });
    // 先查就诊人姓名（用于顶部筛选条显示）
    api.getPatient(idCard)
      .then(p => {
        const name = (p && p.patient_name) || '';
        wx.setStorageSync('patient_name', name);
        this.setData({ patientName: name });
        return api.myRegistrations(idCard);
      })
      .then(list => {
        this.setData({
          registrations: list || [],
          patientId: idCard,
          loading: false
        });
        this.applyFilter();
      })
      .catch(() => this.setData({ loading: false }));
  },

  // 就诊人切换：重新输入身份证
  onPickPatient() {
    this.setData({ patientId: null, registrations: [], filtered: [] });
  },

  // 时间范围切换
  onPickRange() {
    const labels = RANGES.map(r => r.label);
    wx.showActionSheet({
      itemList: labels,
      success: (res) => {
        this.setData({ rangeLabel: labels[res.tapIndex] });
        this.applyFilter();
      }
    });
  },

  // 按时间范围过滤（按就诊日期 work_date / reg_date）
  applyFilter() {
    const range = RANGES.find(r => r.label === this.data.rangeLabel) || RANGES[3];
    if (!range.days) {
      this.setData({ filtered: this.data.registrations });
      return;
    }
    const deadline = new Date();
    deadline.setDate(deadline.getDate() - range.days);
    const filtered = this.data.registrations.filter(item => {
      const d = new Date((item.work_date || item.reg_date || '').replace(/-/g, '/'));
      return !isNaN(d) && d >= deadline;
    });
    this.setData({ filtered });
  },

  statusClass(status) {
    const map = {
      '待就诊': 'tag-blue',
      '就诊中': 'tag-orange',
      '已就诊': 'tag-green',
      '已退号': 'tag-gray'
    };
    return map[status] || 'tag-gray';
  },

  onCancel(e) {
    const id = e.currentTarget.dataset.id;
    const item = this.data.registrations.find(r => String(r.registration_id) === String(id));
    if (!item) return;
    if (item.visit_status !== '待就诊') {
      wx.showToast({ title: '仅待就诊状态的挂号可退号', icon: 'none' });
      return;
    }
    wx.showModal({
      title: '确认退号',
      content: `确定退掉 ${item.department_name}·${item.doctor_name} 的挂号吗？挂号费将退回。`,
      confirmText: '退号',
      confirmColor: '#e64340',
      success: (res) => {
        if (!res.confirm) return;
        wx.showLoading({ title: '退号中…' });
        api.cancelRegistration(id)
          .then(() => {
            wx.hideLoading();
            wx.showToast({ title: '退号成功', icon: 'success' });
            this.search();
          })
          .catch(() => wx.hideLoading());
      }
    });
  }
});
