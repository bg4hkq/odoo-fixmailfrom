# -*- coding: utf-8 -*-
{
    'name': "odoo-fixmailfrom",

    'summary': """
       修正odoo发送邮件时邮件头的form字段问题
       """,

    'description': """
        修正odoo发送邮件时邮件头的form字段问题,比如QQ邮箱和163、263的，就有这种讨厌的问题。
        服务器要求发件人和登录账户必须一致，而系统会根据发件人进行替换，假装你通知对方，所以这个模块解决这个问题。
        另外，如果是频道的话，alias会跟频道同名，这样回复过来的邮件就会没法自动分配到相应的频道，解决办法就是去服务器上给登录用户设置别名。
        163的不支持别名，但是可以支持邮件列表，设置一个邮件列表，并给设置唯一成员为你的登录用户即可。
        有问题请反馈到yurihuang@me.com
    """,

    'author': "Yuri<yurihuang@me.com>",
    'website': "https://baoz.info",

    # Categories can be used to filter modules in modules listing
    # Check https://github.com/odoo/odoo/blob/master/odoo/addons/base/module/module_data.xml
    # for the full list
    'category': 'base',
    "version": "10.0.1.0.0",

    # any module necessary for this one to work correctly
    'depends': ['base'],
    'installable': True,
    'application': True,

}