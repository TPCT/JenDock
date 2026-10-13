pipeline {
    agent any
    parameters {
        string(name: 'VERSION', defaultValue: '1.0', description: 'Release version')
        booleanParam(name: 'DRY_RUN', defaultValue: false, description: 'Skip deploy')
        choice(name: 'REGION', choices: ['eu', 'us', 'ap'], description: 'Target region')
    }
    stages {
        stage('Show') {
            steps {
                echo "Version ${params.VERSION} in ${params.REGION} (dry run: ${params.DRY_RUN})"
            }
        }
    }
}
