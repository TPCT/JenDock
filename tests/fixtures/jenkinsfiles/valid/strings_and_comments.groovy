/* Block comment
   spanning lines */
pipeline {
    agent { docker { image 'ubuntu:24.04' } } // trailing comment
    environment {
        GREETING = 'it\'s a "test"'
        PRICE = "cost: \$5"
        PATH_LIKE = "${GREETING}/bin:$GREETING"
        FROM_ENV = "${env.GREETING}"
    }
    stages {
        stage('Multi-line') {
            steps {
                sh '''
                    set -e
                    echo 'quoted' && echo "$HOME"
                '''
                sh """
                    echo ${GREETING}
                    echo \$HOME
                """
                echo 'tab\there\nnewline \\ backslash'
                sh script: 'ls',
                   label: 'list'
                sh(
                    script: 'pwd',
                    label: "where ${BUILD_NUMBER}",
                )
            }
        }
    }
}
