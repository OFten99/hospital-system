// pages/mine/mine.js —— 个人中心
Page({
  data: {
    userName: '就诊人'
  },

  onShow() {
    const name = wx.getStorageSync('patient_name');
    if (name) this.setData({ userName: name });
  },

  goRecords() {
    wx.navigateTo({ url: '/pages/records/records' });
  },

  onSetting() {
    wx.showToast({ title: '演示版暂未开放', icon: 'none' });
  }
});
