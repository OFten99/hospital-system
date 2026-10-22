// pages/schedules/schedules.js —— 排班与号源选择
const api = require('../../utils/request.js');

Page({
  data: {
    doctorId: null,
    doctorName: '',
    title: '',
    schedules: [],
    loading: true,
    selectedIndex: -1
  },

  onLoad(options) {
    this.setData({
      doctorId: options.doctor_id,
      doctorName: options.doctor_name || '',
      title: options.title || ''
    });
    wx.setNavigationBarTitle({ title: '选择号源' });
    this.loadSchedules();
  },

  loadSchedules() {
    this.setData({ loading: true });
    api.getSchedules(this.data.doctorId, 7)
      .then(list => this.setData({ schedules: list || [], loading: false }))
      .catch(() => this.setData({ loading: false }));
  },

  onSelect(e) {
    const idx = e.currentTarget.dataset.index;
    const item = this.data.schedules[idx];
    if (item.remain <= 0) return; // 无号源不可选
    this.setData({ selectedIndex: idx });
  },

  onNext() {
    const { schedules, selectedIndex, doctorId, doctorName, title } = this.data;
    if (selectedIndex < 0) {
      wx.showToast({ title: '请先选择号源', icon: 'none' });
      return;
    }
    const s = schedules[selectedIndex];
    wx.navigateTo({
      url: `/pages/register/register?schedule_id=${s.schedule_id}&doctor_id=${doctorId}&doctor_name=${doctorName}&title=${title}&work_date=${s.work_date}&shift_type=${s.shift_type}&fee=${s.max_registrations ? '' : ''}`
    });
  }
});
