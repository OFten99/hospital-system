// pages/doctors/doctors.js —— 某科室医生列表
const api = require('../../utils/request.js');

Page({
  data: {
    departmentId: null,
    departmentName: '',
    doctors: [],
    loading: true
  },

  onLoad(options) {
    this.setData({
      departmentId: options.department_id,
      departmentName: options.department_name || ''
    });
    wx.setNavigationBarTitle({ title: options.department_name || '选择医生' });
    this.loadDoctors();
  },

  loadDoctors() {
    this.setData({ loading: true });
    api.getDoctors(this.data.departmentId)
      .then(list => this.setData({ doctors: list || [], loading: false }))
      .catch(() => this.setData({ loading: false }));
  },

  onTapDoctor(e) {
    const { id, name, title } = e.currentTarget.dataset;
    wx.navigateTo({
      url: `/pages/schedules/schedules?doctor_id=${id}&doctor_name=${name}&title=${title}`
    });
  }
});
