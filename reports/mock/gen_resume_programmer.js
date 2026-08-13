/**
 * Mock resume: 计算机程序设计员（DOCX）
 * Usage: node reports/mock/gen_resume_programmer.js
 */
const fs = require("fs");
const path = require("path");
const {
  Document,
  Packer,
  Paragraph,
  TextRun,
  Table,
  TableRow,
  TableCell,
  AlignmentType,
  LevelFormat,
  BorderStyle,
  WidthType,
  ShadingType,
  VerticalAlign,
} = require("docx");

const out = path.join(__dirname, "简历-计算机程序设计员-张启明.docx");

// A4 content width ~ 9026 with ~0.7" margins; use 0.75" = 1080 → content 9746? 
// A4 11906, margin 720 (0.5") → content 10466
const PAGE_W = 11906;
const PAGE_H = 16838;
const MARGIN = 720;
const CONTENT_W = PAGE_W - MARGIN * 2; // 10466

const noBorder = {
  top: { style: BorderStyle.NONE, size: 0, color: "FFFFFF" },
  bottom: { style: BorderStyle.NONE, size: 0, color: "FFFFFF" },
  left: { style: BorderStyle.NONE, size: 0, color: "FFFFFF" },
  right: { style: BorderStyle.NONE, size: 0, color: "FFFFFF" },
};
const thin = { style: BorderStyle.SINGLE, size: 4, color: "CBD5E1" };
const thinB = { top: thin, bottom: thin, left: thin, right: thin };

function sectionTitle(text) {
  return new Paragraph({
    spacing: { before: 280, after: 120 },
    border: {
      bottom: { style: BorderStyle.SINGLE, size: 12, color: "1E3A5F", space: 4 },
    },
    children: [
      new TextRun({
        text,
        bold: true,
        size: 24,
        font: "Microsoft YaHei",
        color: "1E3A5F",
      }),
    ],
  });
}

function body(text, opts = {}) {
  return new Paragraph({
    spacing: { after: 60, line: 276 },
    children: [
      new TextRun({
        text,
        size: 20,
        font: "Microsoft YaHei",
        color: "1F2937",
        ...opts,
      }),
    ],
  });
}

function metaLine(label, value) {
  return new Paragraph({
    spacing: { after: 40 },
    children: [
      new TextRun({
        text: label,
        size: 18,
        font: "Microsoft YaHei",
        color: "64748B",
      }),
      new TextRun({
        text: value,
        size: 18,
        font: "Microsoft YaHei",
        color: "334155",
      }),
    ],
  });
}

function bullet(text, ref = "bullets") {
  return new Paragraph({
    numbering: { reference: ref, level: 0 },
    spacing: { after: 40, line: 276 },
    children: [
      new TextRun({
        text,
        size: 20,
        font: "Microsoft YaHei",
        color: "1F2937",
      }),
    ],
  });
}

function jobHeader(company, title, period) {
  return new Paragraph({
    spacing: { before: 140, after: 40 },
    children: [
      new TextRun({
        text: company,
        bold: true,
        size: 21,
        font: "Microsoft YaHei",
        color: "0F172A",
      }),
      new TextRun({
        text: "  |  ",
        size: 20,
        font: "Microsoft YaHei",
        color: "94A3B8",
      }),
      new TextRun({
        text: title,
        size: 20,
        font: "Microsoft YaHei",
        color: "1E40AF",
      }),
      new TextRun({
        text: "  " + period,
        size: 18,
        font: "Microsoft YaHei",
        color: "64748B",
      }),
    ],
  });
}

function skillCell(title, skills) {
  return new TableCell({
    borders: thinB,
    width: { size: Math.floor(CONTENT_W / 2), type: WidthType.DXA },
    shading: { fill: "F8FAFC", type: ShadingType.CLEAR },
    margins: { top: 80, bottom: 80, left: 120, right: 120 },
    children: [
      new Paragraph({
        spacing: { after: 40 },
        children: [
          new TextRun({
            text: title,
            bold: true,
            size: 18,
            font: "Microsoft YaHei",
            color: "1E3A5F",
          }),
        ],
      }),
      new Paragraph({
        children: [
          new TextRun({
            text: skills,
            size: 17,
            font: "Microsoft YaHei",
            color: "334155",
          }),
        ],
      }),
    ],
  });
}

const doc = new Document({
  styles: {
    default: {
      document: {
        run: { font: "Microsoft YaHei", size: 20 },
      },
    },
  },
  numbering: {
    config: [
      {
        reference: "bullets",
        levels: [
          {
            level: 0,
            format: LevelFormat.BULLET,
            text: "•",
            alignment: AlignmentType.LEFT,
            style: {
              paragraph: { indent: { left: 420, hanging: 240 } },
            },
          },
        ],
      },
      {
        reference: "bullets2",
        levels: [
          {
            level: 0,
            format: LevelFormat.BULLET,
            text: "•",
            alignment: AlignmentType.LEFT,
            style: {
              paragraph: { indent: { left: 420, hanging: 240 } },
            },
          },
        ],
      },
      {
        reference: "bullets3",
        levels: [
          {
            level: 0,
            format: LevelFormat.BULLET,
            text: "•",
            alignment: AlignmentType.LEFT,
            style: {
              paragraph: { indent: { left: 420, hanging: 240 } },
            },
          },
        ],
      },
    ],
  },
  sections: [
    {
      properties: {
        page: {
          size: { width: PAGE_W, height: PAGE_H },
          margin: {
            top: MARGIN,
            right: MARGIN,
            bottom: MARGIN,
            left: MARGIN,
          },
        },
      },
      children: [
        // Header name
        new Paragraph({
          alignment: AlignmentType.CENTER,
          spacing: { after: 80 },
          children: [
            new TextRun({
              text: "张启明",
              bold: true,
              size: 40,
              font: "Microsoft YaHei",
              color: "0F172A",
            }),
          ],
        }),
        new Paragraph({
          alignment: AlignmentType.CENTER,
          spacing: { after: 60 },
          children: [
            new TextRun({
              text: "求职意向：计算机程序设计员 / 后端开发工程师",
              size: 20,
              font: "Microsoft YaHei",
              color: "1E40AF",
            }),
          ],
        }),
        new Paragraph({
          alignment: AlignmentType.CENTER,
          spacing: { after: 40 },
          border: {
            bottom: {
              style: BorderStyle.SINGLE,
              size: 8,
              color: "E2E8F0",
              space: 8,
            },
          },
          children: [
            new TextRun({
              text: "男  |  27岁  |  本科  |  3年工作经验  |  杭州",
              size: 17,
              font: "Microsoft YaHei",
              color: "64748B",
            }),
          ],
        }),
        new Paragraph({
          alignment: AlignmentType.CENTER,
          spacing: { after: 120 },
          children: [
            new TextRun({
              text: "手机：138-0000-6288    邮箱：zhangqiming.mock@example.com    微信：zqm_dev_mock",
              size: 16,
              font: "Microsoft YaHei",
              color: "64748B",
            }),
          ],
        }),

        // 个人优势
        sectionTitle("一、个人优势"),
        bullet(
          "熟悉 Java / Python 后端开发，能独立完成业务接口设计、实现与联调。"
        ),
        bullet(
          "掌握 MySQL / PostgreSQL 与 Redis，具备索引优化、慢查询排查与基础缓存设计经验。"
        ),
        bullet(
          "了解微服务与容器化部署（Spring Boot / Docker），能配合 CI/CD 完成发布与问题定位。"
        ),
        bullet(
          "有电商与 SaaS 后台项目经验，注重代码规范、单元测试与接口文档。"
        ),

        // 工作经历
        sectionTitle("二、工作经历"),
        jobHeader(
          "杭州云栈科技有限公司",
          "后端开发工程师（程序设计）",
          "2023.07 — 至今"
        ),
        body("主要负责订单与库存相关后端服务，参与需求评审、技术方案与线上问题处理。"),
        bullet("负责订单中心 REST API 开发与重构，核心下单接口平均响应从 280ms 优化到 120ms。", "bullets2"),
        bullet("设计库存扣减幂等方案（Redis + DB 事务），支撑大促峰值约 800 QPS 无超卖。", "bullets2"),
        bullet("编写接口文档与集成测试，推动前后端契约对齐，联调周期缩短约 30%。", "bullets2"),
        bullet("参与 Jenkins + Docker 部署流水线改造，实现测试环境一键发布。", "bullets2"),

        jobHeader(
          "杭州启航网络科技有限公司",
          "初级 Java 开发工程师",
          "2022.07 — 2023.06"
        ),
        body("参与内部运营后台与数据报表模块开发，完成 CRUD 业务与定时任务。"),
        bullet("使用 Spring Boot + MyBatis 实现商户管理、权限与操作日志模块。", "bullets3"),
        bullet("开发日报/周报统计任务，基于 XXL-JOB 调度，减少人工导出工作量。", "bullets3"),
        bullet("排查生产空指针与连接池耗尽问题，补充监控与告警阈值。", "bullets3"),

        // 项目经验
        sectionTitle("三、项目经验"),
        jobHeader("订单履约中台（公司项目）", "后端核心开发", "2024.01 — 2025.06"),
        body(
          "技术栈：Java 17、Spring Boot 3、PostgreSQL、Redis、RabbitMQ、Docker。"
        ),
        bullet(
          "拆分下单、支付回调、履约状态机三个服务边界，沉淀统一错误码与幂等键规范。"
        ),
        bullet(
          "实现延迟关单与补偿任务，异常订单人工介入率下降约 40%。"
        ),
        bullet(
          "配合前端完成管理台筛选与导出接口，支持百万级订单分页查询（游标 + 索引）。"
        ),

        jobHeader("校园二手交易小程序（毕业设计扩展）", "全栈负责人", "2021.09 — 2022.05"),
        body("技术栈：Python Flask、MySQL、微信小程序、OSS。"),
        bullet("完成商品发布、聊天、下单与评价全流程；日活测试用户约 200。"),
        bullet("实现图片压缩上传与敏感词过滤；输出部署手册与接口说明。"),

        // 专业技能
        sectionTitle("四、专业技能"),
        new Table({
          width: { size: CONTENT_W, type: WidthType.DXA },
          columnWidths: [Math.floor(CONTENT_W / 2), Math.ceil(CONTENT_W / 2)],
          rows: [
            new TableRow({
              children: [
                skillCell(
                  "编程语言",
                  "Java（熟练）、Python（熟练）、JavaScript/TypeScript（了解）、SQL（熟练）"
                ),
                skillCell(
                  "框架与中间件",
                  "Spring Boot、MyBatis、Flask、Redis、RabbitMQ、Nginx"
                ),
              ],
            }),
            new TableRow({
              children: [
                skillCell(
                  "数据与存储",
                  "MySQL、PostgreSQL、索引与执行计划、基础分库分表概念"
                ),
                skillCell(
                  "工程与工具",
                  "Git、Maven、Docker、Jenkins、Linux 常用命令、Postman、Swagger"
                ),
              ],
            }),
          ],
        }),

        // 教育背景
        sectionTitle("五、教育背景"),
        new Paragraph({
          spacing: { before: 80, after: 40 },
          children: [
            new TextRun({
              text: "浙江某工业大学",
              bold: true,
              size: 21,
              font: "Microsoft YaHei",
              color: "0F172A",
            }),
            new TextRun({
              text: "  |  计算机科学与技术  |  本科  |  2018.09 — 2022.06",
              size: 19,
              font: "Microsoft YaHei",
              color: "475569",
            }),
          ],
        }),
        bullet("主修：数据结构、操作系统、计算机网络、数据库系统、软件工程。"),
        bullet("GPA：3.4 / 4.0；校级程序设计竞赛三等奖（2020）。"),

        // 证书与其他
        sectionTitle("六、证书与其他"),
        bullet("计算机技术与软件专业技术资格：软件设计师（中级，2023）。"),
        bullet("英语：CET-6；可阅读英文技术文档。"),
        bullet("自我评价：踏实稳重，善于排查问题，能在节奏较快的业务迭代中交付可用接口。"),

        new Paragraph({
          spacing: { before: 360 },
          alignment: AlignmentType.CENTER,
          children: [
            new TextRun({
              text: "【本文件为测试用模拟简历，人物与公司均为虚构】",
              size: 16,
              font: "Microsoft YaHei",
              color: "94A3B8",
              italics: true,
            }),
          ],
        }),
      ],
    },
  ],
});

Packer.toBuffer(doc).then((buffer) => {
  fs.writeFileSync(out, buffer);
  console.log("OK", out);
});
