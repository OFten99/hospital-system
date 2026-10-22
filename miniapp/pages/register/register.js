// pages/register/register.js —— 填写就诊人信息并提交挂号
const api = require('../../utils/request.js');

Page({
  data: {
    scheduleId: null,
    doctorName: '',
    title: '',
    workDate: '',
    shiftType: '',
    regType: '普通号',
    payMethod: '微信',
    // 就诊人表单
    name: '',
    gender: '男',
    birthDate: '',
    idCard: '',
    phone: '',
    submitting: false
  },

  onLoad(options) {
    this.setData({
      scheduleId: options.schedule_id,
      doctorName: options.doctor_name || '',
      title: options.title || '',
      workDate: options.work_date || '',
      shiftType: options.shift_type || ''
    });
    wx.setNavigationBarTitle({ title: '确认挂号' });
  },

  onInput(e) {
    const field = e.currentTarget.dataset.field;
    this.setData({ [field]: e.detail.value });
  },

  onGenderChange(e) {
    this.setData({ gender: e.detail.value });
  },

  onBirthDateChange(e) {
    this.setData({ birthDate: e.detail.value });
  },

  onRegTypeChange(e) {
    this.setData({ regType: e.detail.value });
  },

  onPayMethodChange(e) {
    this.setData({ payMethod: e.detail.value });
  },

  validate() {
    const { name, idCard, phone } = this.data;
    if (!name.trim()) return '请填写就诊人姓名';
    if (!idCard.trim()) return '请填写身份证号';
    if (!phone.trim()) return '请填写联系电话';
    return '';
  },

  onSubmit() {
    if (this.data.submitting) return;
    const err = this.validate();
    if (err) {
      wx.showToast({ title: err, icon: 'none' });
      return;
    }

    const { scheduleId, regType, payMethod, name, gender, birthDate, idCard, phone } = this.data;
    this.setData({ submitting: true });

    const payload = {
      schedule_id: Number(scheduleId),
      reg_type: regType,
      pay_method: payMethod,
      patient: {
        name: name.trim(),
        gender,
        birth_date: birthDate || null,
        id_card: idCard.trim(),
        phone: phone.trim()
      }
    };

    api.register(payload)
      .then(res => {
        wx.showModal({
          title: '挂号成功',
          content: `挂号单号：${res.reg_no}\n排队号：${res.queue_no}\n挂号费：¥${res.reg_fee}`,
          showCancel: false,
          confirmText: '好的',
          success: () => {
            wx.switchTab({ url: '/pages/mine/mine' });
          }
        });
      })
      .catch(() => {})
      .then(() => this.setData({ submitting: false }));
  }
});
